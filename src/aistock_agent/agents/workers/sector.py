"""Sector Analyst Agent — 板块分析

工具集：get_leader_stocks, get_capital_flow
"""

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.sector import SECTOR_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """板块分析：龙头筛选 + 资金动向

    tag_code（BK 码）由 escalate 解析后传入（D22/D24）；缺失时行为不变
    （回落 skill 由 escalate 处理，worker 侧不做回落逻辑）。
    """
    tag_code = state.get("tag_code")
    # 注入目标板块上下文，避免 prompt 仅靠 messages 语义猜测板块
    system_content = SECTOR_ANALYST_PROMPT
    if tag_code:
        system_content = f"目标板块代码：{tag_code}（BK 码），分析该板块。\n\n{system_content}"

    try:
        llm = get_deep_think()
        tools = get_tools("sector")
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=system_content),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        return {"final_response": final_response}
    except Exception as e:
        # agent 层最后防线：捕获 LLM/Graph 框架异常（工具异常已被 safe_tool_call 降级）
        logger.error(
            "agent_run_failed",
            agent="sector_analyst",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "板块分析暂时不可用，请稍后重试"}
