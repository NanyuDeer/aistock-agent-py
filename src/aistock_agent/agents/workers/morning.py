"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时
"""

from collections.abc import AsyncGenerator
from datetime import datetime

import redis.asyncio as aioredis
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.config import settings
from aistock_agent.constants import SSEEventType
from aistock_agent.prompts.workers.morning import MORNING_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.market_tools import get_global_markets, tavily_finance_search
from aistock_agent.tools.news_tools import get_cls_news
from aistock_agent.utils.date import is_trading_day  # 亦作为模块属性供 test_morning_agent.py patch
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.sse import map_langgraph_event_to_sse


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
    tools = [tavily_finance_search, get_global_markets, get_cls_news]
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
    today = datetime.now().strftime("%Y年%m月%d日")

    # 检查缓存
    cached = await _get_cached_briefing()
    if cached:
        return {"final_response": cached}

    # 构建提示词
    system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)

    # 创建 ReAct Agent
    llm = get_deep_think()
    tools = [tavily_finance_search, get_global_markets, get_cls_news]
    agent = create_react_agent(llm, tools)

    # 执行
    result = await agent.ainvoke(
        {"messages": [SystemMessage(content=system_prompt)]},
    )

    # 提取最终响应（与其他 4 个 agent 统一使用 extract_final_ai_response）
    final_response = extract_final_ai_response(result.get("messages", []))

    # 缓存结果
    if final_response:
        await _set_cached_briefing(final_response)

    return {"final_response": final_response}


async def _get_cached_briefing() -> str | None:
    """从 Redis 获取缓存晨报"""
    try:
        client = aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:morning:{today}"
        cached = await client.get(cache_key)
        await client.aclose()
        if cached:
            return cached.decode() if isinstance(cached, bytes) else cached
    except Exception:
        pass
    return None


async def _set_cached_briefing(content: str, ttl: int = 7200) -> None:
    """缓存晨报到 Redis，TTL=2小时"""
    try:
        client = aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:morning:{today}"
        await client.setex(cache_key, ttl, content)
        await client.aclose()
    except Exception:
        pass
