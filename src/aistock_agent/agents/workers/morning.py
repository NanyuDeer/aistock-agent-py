"""Morning Agent — 晨报宏观分析（最高优先级）

模式：create_react_agent，LLM 自主决定搜索策略
工具集：tavily_finance_search, get_global_markets, get_cls_news
缓存：Redis TTL=2小时（通过 services.cache → RedisPool 单例）
归档：docs/agent-outputs/morning/YYYY-MM-DD-HHMM-briefing.md

流式：由 graph 层 ``astream_events(v2)`` 自动提供，agent 不关心传输协议。
"""

from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.morning import MORNING_PROMPT
from aistock_agent.services.archiver import archive_morning
from aistock_agent.services.cache import get_cached_briefing, set_cached_briefing
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.date import is_trading_day
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.output_parser import extract_major_events

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """晨报分析：宏观策略4步框架 — cache → create_react_agent → ainvoke → extract → cache+archive

    流式由 graph 层 ``astream_events(v2)`` 提供，agent 不关心传输协议。
    """
    try:
        today = datetime.now().strftime("%Y年%m月%d日")

        # 检查缓存
        cached = await get_cached_briefing()
        if cached:
            major_events = extract_major_events(cached)
            return {
                "final_response": cached,
                "analysis_reports": {
                    **state.get("analysis_reports", {}),
                    "major_events": major_events,
                },
            }

        # 构建提示词
        system_prompt = MORNING_PROMPT.replace("{{DATE}}", today)
        if not is_trading_day():
            system_prompt += (
                "\n\n注意：今日为非交易日（周末或节假日），"
                "请在报告开头注明，分析可聚焦于下一交易日前瞻。"
            )

        # 创建 ReAct Agent
        llm = get_deep_think()
        tools = get_tools("morning")
        agent = create_react_agent(llm, tools)

        # 执行
        result = await agent.ainvoke(
            {"messages": [SystemMessage(content=system_prompt)]},
        )

        # 提取最终响应
        final_response = extract_final_ai_response(result.get("messages", []))

        # 提取 major_events（供 event agent 消费）
        major_events = extract_major_events(final_response)
        if major_events:
            logger.info(
                "morning_major_events_extracted",
                count=len(major_events),
                titles=[str(e.get("title", ""))[:30] for e in major_events],
            )

        # 缓存 + 归档（供 snapshot_builder 读取）
        if final_response:
            await set_cached_briefing(final_response)
            archive_morning(final_response)
            # 持久化到数据库（scheduler 触发时，供 broadcast_agent 等下游读取）
            if state.get("trigger_source") == "scheduler":
                report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
                await node_api.save_analysis_report(
                    report_type="morning",
                    report_date=report_date,
                    content={"text": final_response},
                )

        return {
            "final_response": final_response,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "major_events": major_events,
            },
        }
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="morning",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "晨报生成暂时不可用，请稍后重试"}
