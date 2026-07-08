"""长线风口 Agent — 风口趋势分析

工具集：get_wind_leaders（从 sector_tools 注册到 wind_leader category）
模型：deep_think（多维度风口研判）
"""

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.workers.wind_leader import WIND_LEADER_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response

logger = get_logger(__name__)


async def run(state: AgentState) -> dict[str, object]:
    """长线风口分析：热门板块 + 龙头股"""
    try:
        llm = get_deep_think()
        tools = get_tools("wind_leader")
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=WIND_LEADER_ANALYST_PROMPT),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        # 写入 analysis_reports 供 broadcast_agent 使用
        return {
            "final_response": final_response,
            "analysis_reports": {**state.get("analysis_reports", {}), "wind_leader": final_response},
        }
    except Exception as e:
        logger.error("agent_run_failed", agent="wind_leader", error=str(e), exc_info=True)
        return {"final_response": "长线风口分析暂时不可用，请稍后重试"}