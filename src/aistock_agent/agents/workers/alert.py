"""Alert Agent — 异动提醒分析

模式：create_react_agent，deep_think
工具集：get_stock_monitor, get_alert_history, get_quote, get_capital_flow, search_cls_news
三步框架：发生了什么 → 为什么 → 怎么办，按短/中/长线分类
"""

from collections.abc import AsyncGenerator

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.constants import SSEEventType
from aistock_agent.prompts.workers.alert import ALERT_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.sse import map_langgraph_event_to_sse

logger = structlog.get_logger()

# cycle 参数值 → 中文标签映射（SSE 接口传英文，prompt 用中文）
_CYCLE_MAP: dict[str, str] = {
    "short": "短线",
    "mid": "中线",
    "long": "长线",
}


async def stream(state: dict[str, object]) -> AsyncGenerator[dict[str, object], None]:
    """异动提醒 SSE 流：走 ReAct + astream_events，实时推送分析进度"""
    symbol = str(state.get("symbol") or "")
    cycle_raw = str(state.get("cycle", state.get("tag_code", "")))
    cycle_label = _CYCLE_MAP.get(cycle_raw, cycle_raw)

    user_msg = f"分析 {symbol} 的异动情况"
    if cycle_label:
        user_msg += f"，关注{cycle_label}周期"

    llm = get_deep_think()
    tools = get_tools("alert")
    react_agent = create_react_agent(llm, tools)

    _llm_started = False

    try:
        async for event in react_agent.astream_events(
            {
                "messages": [
                    SystemMessage(content=ALERT_ANALYST_PROMPT),
                    HumanMessage(content=user_msg),
                ]
            },
            version="v2",
        ):
            sse_event = map_langgraph_event_to_sse(event)
            if sse_event is None:
                continue

            event_t = sse_event.get("type")
            if event_t in (SSEEventType.TOOL_START, SSEEventType.TOOL_END):
                yield sse_event
            elif event_t == SSEEventType.TEXT:
                if not _llm_started:
                    _llm_started = True
                    yield {"type": SSEEventType.LLM_START, "label": "正在生成异动分析"}
                yield sse_event

        yield {"type": SSEEventType.DONE}
    except Exception as e:
        logger.error("alert_stream_failed", symbol=symbol, error=str(e), exc_info=True)
        yield {"type": SSEEventType.ERROR, "message": str(e)}


async def run(state: AgentState) -> dict[str, object]:
    """异动提醒分析：三步框架，按短/中/长线分类

    读取 state.symbol 确定分析目标，无 symbol 时返回提示。
    """
    symbol = state.get("symbol")
    if not symbol:
        return {"final_response": "请提供股票代码，例如：分析一下 600519 的异动"}

    try:
        llm = get_deep_think()
        tools = get_tools("alert")
        agent = create_react_agent(llm, tools)

        # 读取 cycle（短/中/长线），通过 tag_code 字段透传
        cycle_raw = str(state.get("tag_code", ""))
        cycle_label = _CYCLE_MAP.get(cycle_raw, cycle_raw)
        user_msg = f"分析 {symbol} 的异动情况"
        if cycle_label:
            user_msg += f"，关注{cycle_label}周期"

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=ALERT_ANALYST_PROMPT),
                    HumanMessage(content=user_msg),
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        return {"final_response": final_response}
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="alert_agent",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "异动提醒暂时不可用，请稍后重试"}
