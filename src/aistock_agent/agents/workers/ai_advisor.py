"""AI 投顾 Agent — 智能投顾对话节点

用户对话触发时，优先从数据库读取已有分析报告整理汇总回复。
降级策略：DB 无报告 → 使用工具获取数据 → LLM 生成回复。
回复必须简洁（200 字以内），适合手机端显示。
"""

from datetime import datetime

import structlog
from langchain_core.messages import SystemMessage
from langgraph.prebuilt import create_react_agent

from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.tools.registry import get_tools
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.report_parser import extract_display_report

logger = structlog.get_logger()

# intent → report_type 映射
INTENT_REPORT_MAP: dict[str, str] = {
    "morning": "morning",
    "wind_leader": "wind_leader",
    "hot_burst": "hot_burst",
    "stock": "stock",
    "sector": "sector",
}

# 综合咨询时查询的公共报告类型
_GENERAL_REPORT_TYPES: list[str] = ["morning", "wind_leader", "hot_burst"]

# 报告中文标签
_REPORT_LABELS: dict[str, str] = {
    "morning": "晨报",
    "wind_leader": "风口",
    "hot_burst": "热门股",
    "stock": "个股",
    "sector": "板块",
}

# 报告截取最大字符数（避免超长 prompt）
_REPORT_TRUNCATE_LENGTH = 1500


async def _fetch_relevant_reports(
    intent: str, report_date: str
) -> dict[str, str]:
    """从数据库读取与用户意图相关的分析报告

    Args:
        intent: 用户意图
        report_date: 报告日期 (YYYY-MM-DD)

    Returns:
        报告字典 {report_type: report_text}
    """
    reports: dict[str, str] = {}

    if intent in INTENT_REPORT_MAP:
        report_types_to_query = [INTENT_REPORT_MAP[intent]]
    else:
        report_types_to_query = _GENERAL_REPORT_TYPES

    for report_type in report_types_to_query:
        try:
            data = await node_api.get_analysis_report(report_type, report_date)
            if data and isinstance(data.get("content"), dict):
                # 使用 extract_display_report 提取展示文本（兼容 1.0 单层 text 和 2.0 双层 display_report）
                display_text = extract_display_report(data["content"])
                if display_text:
                    reports[report_type] = display_text
        except Exception as e:
            logger.warning(
                "advisor_report_fetch_failed",
                report_type=report_type,
                error=str(e),
            )

    return reports


def _format_available_reports(reports: dict[str, str]) -> str:
    """将报告字典格式化为提示词中的可用报告描述"""
    if not reports:
        return "暂无当日分析报告，请使用工具获取最新数据后回答用户问题。"

    parts: list[str] = []
    for report_type, text in reports.items():
        label = _REPORT_LABELS.get(report_type, report_type)
        truncated = text[:_REPORT_TRUNCATE_LENGTH] + ("..." if len(text) > _REPORT_TRUNCATE_LENGTH else "")
        parts.append(f"### {label}\n{truncated}")

    return "\n\n".join(parts)


async def run(state: AgentState) -> dict[str, object]:
    """智能投顾：优先从 DB 读取报告，降级使用工具获取数据

    流程：
    1. 根据 state.intent 查询数据库中的相关报告
    2. 如果有报告：用 LLM 基于报告整理汇总回复（省 token）
    3. 如果无报告：用 ReAct Agent 调用工具获取数据后回复
    4. 回复直接展示在对话气泡中，简洁要点式排版
    """
    try:
        intent = state.get("intent", "general") or "general"
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")

        # 步骤 1: 查询数据库
        reports = await _fetch_relevant_reports(intent, report_date)
        logger.info(
            "advisor_reports_fetched",
            intent=intent,
            report_date=report_date,
            reports_found=list(reports.keys()),
        )

        # 步骤 2: 构造提示词
        available_reports_text = _format_available_reports(reports)
        prompt = AI_ADVISOR_PROMPT.replace("{{AVAILABLE_REPORTS}}", available_reports_text)

        if reports:
            # 有报告：用 LLM 流式整理汇总（省 token，快速响应，支持逐 token 输出）
            llm = get_deep_think()
            response_chunks: list[str] = []
            async for chunk in llm.astream([
                SystemMessage(content=prompt),
                *state.get("messages", [])[-5:],
            ]):
                if chunk.content:
                    response_chunks.append(
                        chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    )

            final_response = "".join(response_chunks)
            logger.info("advisor_response_from_reports", has_report=True, intent=intent)
        else:
            # 无报告：用 ReAct Agent 调用工具获取数据
            llm = get_deep_think()
            tools = get_tools("advisor")
            agent = create_react_agent(llm, tools)

            result = await agent.ainvoke({
                "messages": [
                    SystemMessage(content=prompt),
                    *state.get("messages", [])[-5:],
                ]
            })

            final_response = extract_final_ai_response(result.get("messages", []))
            logger.info("advisor_response_from_tools", has_report=False, intent=intent)

        return {"final_response": final_response}

    except Exception as e:
        logger.error(
            "agent_run_failed",
            agent="ai_advisor",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": "智能投顾暂时不可用，请稍后重试"}
