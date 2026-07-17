"""Hot Burst Agent — 机构调研热门股 AI 解读（v2.0 双层输出）"""

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
from aistock_agent.utils.report_parser import parse_dual_layer_response

logger = structlog.get_logger()


async def run(state: AgentState) -> dict[str, object]:
    """机构调研热门股分析节点（v2.0 双层输出）"""
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

        report_date = str(state.get("report_date") or datetime.now().strftime("%Y-%m-%d"))

        if final_response:
            # 解析双层输出
            dual = parse_dual_layer_response(final_response)

            # 缓存到本地（前端报告列表查询用）
            try:
                from aistock_agent.services.report_cache import set_report
                set_report("hot_burst", report_date, dual)
            except Exception:
                pass

            # 持久化到数据库（scheduler 触发时）
            if state.get("trigger_source") == "scheduler":
                await node_api.save_analysis_report(
                    report_type="hot_burst",
                    report_date=report_date,
                    content=dual,
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
