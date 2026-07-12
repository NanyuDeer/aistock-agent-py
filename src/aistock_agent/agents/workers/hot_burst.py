"""Hot Burst Agent — 机构调研热门股 AI 解读"""

from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.hot_burst import HOT_BURST_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.hot_burst_tools import get_hot_burst, get_hot_burst_history
from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """机构调研热门股分析节点"""
    try:
        llm = get_deep_think()
        tools = [get_hot_burst, get_hot_burst_history]
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=HOT_BURST_ANALYST_PROMPT),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        # 持久化到数据库（scheduler 触发时，供 broadcast_agent 等下游读取）
        if final_response and state.get("trigger_source") == "scheduler":
            report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
            await node_api.save_analysis_report(
                report_type="hot_burst",
                report_date=report_date,
                content={"text": final_response},
            )

        return {
            "analysis_reports": {"hot_burst": final_response},
            "final_response": final_response,
        }
    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="hot_burst_agent",
            error=str(e),
            exc_info=True,
        )
        degraded = "机构调研热门股分析暂时不可用，请稍后重试"
        return {
            "analysis_reports": {"hot_burst": degraded},
            "final_response": degraded,
        }
