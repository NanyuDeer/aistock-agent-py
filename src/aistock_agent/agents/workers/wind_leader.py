"""长线风口 Agent — 风口趋势分析

工具集：get_wind_leaders（从 sector_tools 注册到 wind_leader category）
模型：deep_think（多维度风口研判）
归档：docs/agent-outputs/wind_leader/YYYY-MM-DD-HHMM-analysis.md
"""

from datetime import datetime
from pathlib import Path

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.observability.logging import get_logger
from aistock_agent.utils.date import shanghai_today
from aistock_agent.prompts.workers.wind_leader import WIND_LEADER_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.data_guard import DataCheck, ensure_data_available
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.report_parser import parse_dual_layer_response

logger = get_logger(__name__)

# 风口分析归档目录
WIND_LEADER_OUTPUT_DIR = Path("docs/agent-outputs/wind_leader")


def _is_wind_leaders_empty(data: dict[str, object] | None) -> bool:
    """检查风口龙头数据是否为空"""
    if not data:
        return True
    sectors = data.get("hot_sectors")
    return not isinstance(sectors, list) or len(sectors) == 0


async def run(state: AgentState) -> dict[str, object]:
    """长线风口分析：热门板块 + 龙头股"""
    try:
        # 预检：确保风口龙头数据可用（scheduler 触发时，最多重试3次）
        # 用户实时请求不预检（避免等待），工具本身有空数据降级处理
        if state.get("trigger_source") == "scheduler":
            checks = [
                DataCheck(
                    check_path="/internal/wind-leaders",
                    refresh_path="/api/cn/wind-leaders/refresh",
                    empty_checker=_is_wind_leaders_empty,
                    name="wind_leaders",
                )
            ]
            if not await ensure_data_available(checks):
                logger.warning("wind_leader_data_unavailable_after_retries")
                return {"final_response": "长线风口分析暂时不可用：后端数据源为空，请稍后重试"}

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

        # 归档到文件（供后续复盘分析使用）
        if final_response:
            _archive_wind_leader(final_response)
            # 持久化到数据库（scheduler 触发时，供 broadcast_agent 等下游读取）
            if state.get("trigger_source") == "scheduler":
                report_date = state.get("report_date") or shanghai_today().isoformat()
                dual_layer_content = parse_dual_layer_response(final_response)
                await node_api.save_analysis_report(
                    report_type="wind_leader",
                    report_date=report_date,
                    data_source="wind_leader_agent",
                    content=dual_layer_content,
                )

        # 写入 analysis_reports 供 broadcast_agent 使用
        return {
            "final_response": final_response,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "wind_leader": final_response,
            },
        }
    except Exception as e:
        logger.error("agent_run_failed", agent="wind_leader", error=str(e), exc_info=True)
        return {"final_response": "长线风口分析暂时不可用，请稍后重试"}


def _archive_wind_leader(content: str) -> None:
    """将风口分析报告归档到文件（供后续复盘分析使用）"""
    try:
        WIND_LEADER_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = WIND_LEADER_OUTPUT_DIR / f"{timestamp}-analysis.md"
        filepath.write_text(content, encoding="utf-8")
        logger.info("wind_leader_archived", path=str(filepath))
    except Exception as e:
        # 归档失败不阻塞主流程
        logger.warning("wind_leader_archive_failed", error=str(e))
