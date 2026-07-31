"""趋势股评分 Agent — K线趋势分析 + 4维度评分解读

工具集：get_trend_score, get_trend_score_detail, get_trend_top_stocks
模型：deep_think（多维度趋势研判）
归档：docs/agent-outputs/trend_score/YYYY-MM-DD-HHMM-analysis.md
"""

from datetime import datetime
from pathlib import Path

from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.workers.trend_score import TREND_SCORE_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.date import shanghai_today
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.report_parser import (
    is_dual_layer_valid,
    parse_dual_layer_response,
    repair_dual_layer_with_llm,
)

logger = get_logger(__name__)

# 趋势股分析归档目录
TREND_SCORE_OUTPUT_DIR = Path("docs/agent-outputs/trend_score")
PERSISTED_TRIGGER_SOURCES = frozenset({"manual", "scheduler"})


async def run(state: AgentState) -> dict[str, object]:
    """趋势股评分分析：4维度评分 + K线趋势解读"""
    try:
        # 预检：scheduler 触发时确保趋势评分 Top 数据可用
        # 用户实时请求不预检（避免等待），工具本身有空数据降级处理
        # 注意：/internal/trend/top 返回列表，用 get_list 而非 get（get 仅接受 dict）
        # 不重试不刷新：趋势评分是每日预计算的，无按需刷新接口
        if state.get("trigger_source") == "scheduler":
            top_data = await node_api.get_list("/internal/trend/top?limit=5")
            if not top_data:
                logger.warning("trend_score_data_unavailable")
                return {"final_response": "趋势股评分分析暂时不可用：评分数据尚未生成，请稍后重试"}

        llm = get_deep_think()
        tools = get_tools("trend_score")
        agent = create_react_agent(llm, tools)

        result = await agent.ainvoke(
            {
                "messages": [
                    SystemMessage(content=TREND_SCORE_ANALYST_PROMPT),
                    *state.get("messages", [])[-5:],
                ]
            }
        )

        final_response = extract_final_ai_response(result.get("messages", []))

        # 归档到文件（供后续复盘分析使用）
        if final_response:
            _archive_trend_score(final_response)
            # 持久化到数据库（手动或 scheduler 触发时，供 broadcast_agent 等下游读取）
            if state.get("trigger_source") in PERSISTED_TRIGGER_SOURCES:
                report_date = state.get("report_date") or shanghai_today().isoformat()
                dual_layer_content = parse_dual_layer_response(final_response)
                if not is_dual_layer_valid(dual_layer_content):
                    logger.info("trend_score_dual_layer_repair_attempt")
                    repaired = await repair_dual_layer_with_llm(final_response)
                    if repaired:
                        dual_layer_content = repaired
                        logger.info("trend_score_dual_layer_repair_success")
                    else:
                        logger.warning("trend_score_dual_layer_repair_failed")
                await node_api.save_analysis_report(
                    report_type="trend_score",
                    report_date=report_date,
                    content=dual_layer_content,
                    data_source="trend_score_agent",
                )

        # 写入 analysis_reports 供 broadcast_agent 使用
        return {
            "final_response": final_response,
            "analysis_reports": {
                **state.get("analysis_reports", {}),
                "trend_score": final_response,
            },
        }
    except Exception as e:
        logger.error("agent_run_failed", agent="trend_score", error=str(e), exc_info=True)
        return {"final_response": "趋势股评分分析暂时不可用，请稍后重试"}


def _archive_trend_score(content: str) -> None:
    """将趋势股分析报告归档到文件（供后续复盘分析使用）"""
    try:
        TREND_SCORE_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
        filepath = TREND_SCORE_OUTPUT_DIR / f"{timestamp}-analysis.md"
        filepath.write_text(content, encoding="utf-8")
        logger.info("trend_score_archived", path=str(filepath))
    except Exception as e:
        # 归档失败不阻塞主流程
        logger.warning("trend_score_archive_failed", error=str(e))
