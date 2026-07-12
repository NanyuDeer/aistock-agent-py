"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时（通过 services.cache → RedisPool 单例）
归档：docs/agent-outputs/morning/YYYY-MM-DD-HHMM-briefing.md
"""

from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.constants import SSEEventType
from aistock_agent.prompts.workers.morning import MORNING_PROMPT
from aistock_agent.services.cache import get_cached_briefing, set_cached_briefing
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.date import is_trading_day  # 亦作为模块属性供 test_morning_agent.py patch
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.sse import map_langgraph_event_to_sse

logger = structlog.get_logger()

# 晨报归档目录（供 snapshot_builder 读取）
MORNING_OUTPUT_DIR = Path("docs/agent-outputs/morning")


async def stream(state: dict[str, object]) -> AsyncGenerator[dict[str, object], None]:
    """晨报 SSE 流：缓存命中直接返回，未命中走 ReAct + astream_events"""
    today = datetime.now().strftime("%Y年%m月%d日")

    cached = await _get_cached_briefing()
    if cached:
        yield {"type": SSEEventType.TEXT, "content": cached}
        yield {"type": SSEEventType.DONE}
        return

    system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
    if not is_trading_day():
        system_prompt += (
            "\n\n注意：今日为非交易日（周末或节假日），"
            "请在报告开头注明，分析可聚焦于下一交易日前瞻。"
        )

    llm = get_deep_think()
    tools = get_tools("morning")
    react_agent = create_react_agent(llm, tools)

    _llm_started = False
    _response_parts: list[str] = []

    try:
        async for event in react_agent.astream_events(
            {"messages": [SystemMessage(content=system_prompt)]},
            version="v2",
        ):
            sse_event = map_langgraph_event_to_sse(event)
            if sse_event is None:
                continue

            event_t = sse_event.get("type")
            if event_t in (SSEEventType.TOOL_START, SSEEventType.TOOL_END):
                yield sse_event
            elif event_t == SSEEventType.TEXT:
                # llm_start 仅在首个文本 chunk 时发射一次（有状态，保留在 stream 内）
                if not _llm_started:
                    _llm_started = True
                    yield {"type": SSEEventType.LLM_START, "label": "正在生成分析报告"}
                yield sse_event
                content = sse_event.get("content")
                if content:
                    _response_parts.append(
                        content if isinstance(content, str) else str(content)
                    )

        final_response = "".join(_response_parts)
        if final_response:
            await _set_cached_briefing(final_response)

    except Exception as e:
        yield {"type": SSEEventType.ERROR, "message": str(e)}
        return

    yield {"type": SSEEventType.DONE}


async def run(state: AgentState) -> dict[str, object]:
    """晨报分析：宏观策略4步框架"""
    try:
        today = datetime.now().strftime("%Y年%m月%d日")

        # 检查缓存
        cached = await _get_cached_briefing()
        if cached:
            return {"final_response": cached}

        # 构建提示词
        system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)

        # 创建 ReAct Agent
        llm = get_deep_think()
        tools = get_tools("morning")
        agent = create_react_agent(llm, tools)

        # 执行
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]},
        )

        # 提取最终响应（与其他 4 个 agent 统一使用 extract_final_ai_response）
        final_response = extract_final_ai_response(result.get("messages", []))

        # 缓存 + 归档（供 snapshot_builder 读取）
        if final_response:
            await _set_cached_briefing(final_response)
            _archive_morning(final_response)
            # 持久化到数据库（scheduler 触发时，供 broadcast_agent 等下游读取）
            if state.get("trigger_source") == "scheduler":
                report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
                await node_api.save_analysis_report(
                    report_type="morning",
                    report_date=report_date,
                    content={"text": final_response},
                )

        return {"final_response": final_response}
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="morning",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "晨报生成暂时不可用，请稍后重试"}


def _archive_morning(content: str) -> None:
    """将晨报报告归档到文件（供 snapshot_builder.build_snapshot() 读取）"""
    try:
        MORNING_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = MORNING_OUTPUT_DIR / f"{timestamp}-briefing.md"
        filepath.write_text(content, encoding="utf-8")
        logger.info("morning_archived", path=str(filepath))
    except Exception as e:
        # 归档失败不阻塞主流程（review agent 同模式）
        logger.warning("morning_archive_failed", error=str(e))


async def _get_cached_briefing() -> str | None:
    """从 Redis 获取缓存晨报（委托给 services.cache → RedisPool）"""
    return await get_cached_briefing()


async def _set_cached_briefing(content: str, ttl: int = 7200) -> None:
    """缓存晨报到 Redis，TTL=2小时（委托给 services.cache → RedisPool）"""
    await set_cached_briefing(content, ttl)
