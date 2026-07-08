"""播报 Agent — 双人对话播报生成

从 state.analysis_reports 集合各 Agent 分析结果，生成 host + analyst 对话。
模型：deep_think（对话式播报生成）
"""

from langchain_core.messages import SystemMessage

from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.workers.broadcast import BROADCAST_ANALYST_PROMPT
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.message import extract_final_ai_response

logger = get_logger(__name__)


async def run(state: AgentState) -> dict[str, object]:
    """播报生成：集合各 Agent 分析结果，生成双人对话播报内容"""
    try:
        # 从 state 中提取各 Agent 的分析结果
        analysis_reports = state.get("analysis_reports", {})

        # 构造提示词（占位符替换）
        prompt = BROADCAST_ANALYST_PROMPT.replace(
            "{{MORNING_BRIEF}}", analysis_reports.get("morning", "暂无晨报")
        ).replace(
            "{{WIND_LEADER}}", analysis_reports.get("wind_leader", "暂无长线风口分析")
        )

        llm = get_deep_think()
        response = await llm.ainvoke([
            SystemMessage(content=prompt),
            {"role": "user", "content": "生成今日播报"},
        ])

        final_response = extract_final_ai_response([response])
        return {"final_response": final_response}
    except Exception as e:
        logger.error("agent_run_failed", agent="broadcast", error=str(e), exc_info=True)
        return {"final_response": "播报生成暂时不可用，请稍后重试"}