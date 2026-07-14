"""Alert Agent — 异动提醒多维分析

架构（Phase 6 重构，2026-07-10）：
  - 3 个子 Agent 并行执行（资讯情报 / 盘口风控 / 图谱发散）
  - Master Agent 汇聚融合 + 操作建议
  - SSE 流：子 Agent 完成后流式输出 Master 结果

模式：asyncio.gather 并行子 Agent → Master ReAct agent → astream_events
"""

import asyncio
from collections.abc import AsyncGenerator

import structlog
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.constants import SSEEventType
from aistock_agent.prompts.workers.alert import (
    GRAPH_DIVERGE_PROMPT,
    MASTER_PROMPT,
    NEWS_INTEL_PROMPT,
    RISK_DIAG_PROMPT,
)
from aistock_agent.services.llm import get_deep_think, get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.sse import map_langgraph_event_to_sse

logger = structlog.get_logger()

# cycle 参数值 → 中文标签映射
_CYCLE_MAP: dict[str, str] = {
    "short": "短线（1-5天）",
    "mid": "中线（1-4周）",
    "long": "长线（1-6月）",
}


def _resolve_cycle(state: dict[str, object]) -> str:
    """从 state 中解析周期中文标签"""
    cycle_raw = str(state.get("cycle", state.get("tag_code", "")))
    return _CYCLE_MAP.get(cycle_raw, "全部周期")


async def _run_sub_agent(
    name: str,
    prompt_template: str,
    tools: list,
    model_type: str,
    symbol: str,
    cycle_label: str,
    user_instruction: str,
) -> str:
    """运行单个子 Agent（非流式），返回 final_response 文本"""
    try:
        llm = get_quick_think() if model_type == "quick" else get_deep_think()
        agent = create_react_agent(llm, tools)
        prompt = prompt_template.format(symbol=symbol, cycle=cycle_label)

        result = await agent.ainvoke({
            "messages": [
                SystemMessage(content=prompt),
                HumanMessage(content=user_instruction),
            ]
        })

        response = extract_final_ai_response(result.get("messages", []))
        return response
    except Exception as e:
        logger.error(
            "alert_sub_agent_failed",
            agent=name,
            symbol=symbol,
            error=str(e),
            exc_info=True,
        )
        return f"[{name}] 分析暂时不可用: {e}"


# ── SSE 流式接口 ──────────────────────────────────────────────────────────────

async def stream(state: dict[str, object]) -> AsyncGenerator[dict[str, object], None]:
    """异动提醒 SSE 流：并行子 Agent → Master 流式输出"""
    symbol = str(state.get("symbol") or "")
    cycle_label = _resolve_cycle(state)

    # ═══════ 阶段 1：并行执行 3 个子 Agent ═══════
    yield {"type": SSEEventType.TOOL_START, "tool": "sub_agents",
           "label": "正在启动多维分析（资讯情报 + 盘口风控 + 图谱发散）"}

    results = await asyncio.gather(
        _run_sub_agent(
            name="资讯情报", prompt_template=NEWS_INTEL_PROMPT, tools=get_tools("alert_news"),
            model_type="quick", symbol=symbol, cycle_label=cycle_label,
            user_instruction=f"查询 {symbol} 的最新资讯，找出异动原因",
        ),
        _run_sub_agent(
            name="盘口风控", prompt_template=RISK_DIAG_PROMPT, tools=get_tools("alert_risk"),
            model_type="deep", symbol=symbol, cycle_label=cycle_label,
            user_instruction=f"分析 {symbol} 的盘口结构和资金面，判断真实意图",
        ),
        _run_sub_agent(
            name="图谱发散",
            prompt_template=GRAPH_DIVERGE_PROMPT,
            tools=get_tools("alert_graph"),
            model_type="quick", symbol=symbol, cycle_label=cycle_label,
            user_instruction=f"以 {symbol} 为中心，用知识图谱寻找产业链补涨标的",
        ),
    )

    news_result, risk_result, graph_result = results

    yield {"type": SSEEventType.TOOL_END, "tool": "sub_agents"}

    # ═══════ 阶段 2：Master Agent 汇聚 ═══════
    yield {"type": SSEEventType.TOOL_START, "tool": "master",
           "label": "正在生成异动深度研判"}

    master_prompt = MASTER_PROMPT.format(symbol=symbol, cycle=cycle_label)
    master_input = f"""请基于以下三份子Agent分析报告，生成 {symbol} 的异动深度研判：

━━━━━━━━━━━━━━━━━━━━━━━━
【资讯情报Agent报告】
{news_result}

【盘口风控Agent报告】
{risk_result}

【图谱发散Agent报告】
{graph_result}
━━━━━━━━━━━━━━━━━━━━━━━━

请按输出格式生成完整研判报告。"""

    llm = get_deep_think()
    master_agent = create_react_agent(llm, [])  # Master 不调用工具，纯融合

    _llm_started = False

    try:
        async for event in master_agent.astream_events(
            {
                "messages": [
                    SystemMessage(content=master_prompt),
                    HumanMessage(content=master_input),
                ]
            },
            version="v2",
        ):
            sse_event = map_langgraph_event_to_sse(event)
            if sse_event is None:
                continue

            event_t = sse_event.get("type")
            if event_t == SSEEventType.TEXT:
                if not _llm_started:
                    _llm_started = True
                    yield {"type": SSEEventType.TOOL_END, "tool": "master"}
                    yield {"type": SSEEventType.LLM_START, "label": "正在生成异动深度研判"}
                yield sse_event

        yield {"type": SSEEventType.DONE}
    except Exception as e:
        logger.error("alert_master_failed", symbol=symbol, error=str(e), exc_info=True)
        yield {"type": SSEEventType.ERROR, "message": f"异动分析生成失败: {e}"}


# ── 非流式接口（Graph 节点用）────────────────────────────────────────────────

async def run(state: AgentState) -> dict[str, object]:
    """异动提醒分析：3 个子 Agent 并行 + Master 汇聚（非流式）"""
    symbol = state.get("symbol")
    if not symbol:
        return {"final_response": "请提供股票代码，例如：分析一下 600519 的异动"}

    cycle_label = _resolve_cycle(dict(state))

    try:
        results = await asyncio.gather(
            _run_sub_agent(
                name="资讯情报", prompt_template=NEWS_INTEL_PROMPT, tools=get_tools("alert_news"),
                model_type="quick", symbol=str(symbol), cycle_label=cycle_label,
                user_instruction=f"查询 {symbol} 的最新资讯，找出异动原因",
            ),
            _run_sub_agent(
                name="盘口风控", prompt_template=RISK_DIAG_PROMPT, tools=get_tools("alert_risk"),
                model_type="deep", symbol=str(symbol), cycle_label=cycle_label,
                user_instruction=f"分析 {symbol} 的盘口结构和资金面，判断真实意图",
            ),
            _run_sub_agent(
                name="图谱发散",
                prompt_template=GRAPH_DIVERGE_PROMPT,
                tools=get_tools("alert_graph"),
                model_type="quick", symbol=str(symbol), cycle_label=cycle_label,
                user_instruction=f"以 {symbol} 为中心，用知识图谱寻找产业链补涨标的",
            ),
        )

        news_result, risk_result, graph_result = results

        master_prompt = MASTER_PROMPT.format(symbol=str(symbol), cycle=cycle_label)
        master_input = f"""请基于以下三份子Agent分析报告，生成 {symbol} 的异动深度研判：

━━━━━━━━━━━━━━━━━━━━━━━━
【资讯情报Agent报告】
{news_result}

【盘口风控Agent报告】
{risk_result}

【图谱发散Agent报告】
{graph_result}
━━━━━━━━━━━━━━━━━━━━━━━━

请按输出格式生成完整研判报告。"""

        llm = get_deep_think()
        master_agent = create_react_agent(llm, [])
        result = await master_agent.ainvoke({
            "messages": [
                SystemMessage(content=master_prompt),
                HumanMessage(content=master_input),
            ]
        })

        final_response = extract_final_ai_response(result.get("messages", []))
        return {"final_response": final_response}
    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="alert_agent",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "异动提醒暂时不可用，请稍后重试"}
