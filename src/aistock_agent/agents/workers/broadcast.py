"""播报 Agent — 双人对话播报生成

从数据库（scheduler 链路）或 state.analysis_reports（实时请求）集合各 Agent 分析结果，
生成 host + analyst 对话，并调用火山引擎播客 API 生成双人语音。
模型：deep_think（对话式播报生成）
"""

from langchain_core.messages import SystemMessage

from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.workers.broadcast import BROADCAST_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.volcengine_podcast import get_podcast_service
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.message import extract_final_ai_response

logger = get_logger(__name__)


async def _fetch_report_from_db(report_type: str, report_date: str) -> str | None:
    """从数据库读取分析报告内容

    Args:
        report_type: 报告类型 (morning/wind_leader/hot_burst)
        report_date: 报告日期 (YYYY-MM-DD)

    Returns:
        报告文本内容，或 None（不存在）
    """
    data = await node_api.get_analysis_report(report_type, report_date)
    if data and isinstance(data.get("content"), dict):
        text = data["content"].get("text")
        if isinstance(text, str) and text:
            return text
    return None


async def run(state: AgentState) -> dict[str, object]:
    """播报生成：集合各 Agent 分析结果，生成双人对话播报内容

    流程：
    1. scheduler 触发时从数据库读取晨报、风口、机构调研报告；
       实时请求时从 state.analysis_reports 读取
    2. 调用 deep_think 生成双人对话文本
    3. 调用火山引擎播客 API 生成双人语音（MVP 功能）
    4. 返回对话文本 + 音频路径

    Returns:
        dict: {"final_response": 对话文本, "audio_path": 音频路径}
    """
    try:
        report_date = state.get("report_date")
        analysis_reports = state.get("analysis_reports", {})

        # scheduler 链路：从数据库读取报告
        morning_report = None
        wind_leader_report = None
        hot_burst_report = None
        if report_date:
            morning_report = await _fetch_report_from_db("morning", report_date)
            wind_leader_report = await _fetch_report_from_db("wind_leader", report_date)
            hot_burst_report = await _fetch_report_from_db("hot_burst", report_date)
            logger.info(
                "broadcast_reports_from_db",
                report_date=report_date,
                has_morning=bool(morning_report),
                has_wind_leader=bool(wind_leader_report),
                has_hot_burst=bool(hot_burst_report),
            )

        # 降级到 state.analysis_reports（实时请求或数据库未命中）
        if not morning_report:
            morning_report = analysis_reports.get("morning", "暂无晨报")
        if not wind_leader_report:
            wind_leader_report = analysis_reports.get("wind_leader", "暂无长线风口分析")
        if not hot_burst_report:
            hot_burst_report = analysis_reports.get("hot_burst", "暂无机构调研分析")

        logger.info(
            "broadcast_agent_start",
            report_date=report_date,
            has_morning=bool(morning_report),
            has_wind_leader=bool(wind_leader_report),
            has_hot_burst=bool(hot_burst_report),
        )

        # 构造提示词（占位符替换）
        prompt = BROADCAST_ANALYST_PROMPT.replace(
            "{{MORNING_BRIEF}}", morning_report
        ).replace(
            "{{WIND_LEADER}}", wind_leader_report
        ).replace(
            "{{HOT_BURST}}", hot_burst_report
        )

        # Step 1: 生成双人对话文本
        llm = get_deep_think()
        response = await llm.ainvoke([
            SystemMessage(content=prompt),
            {"role": "user", "content": "生成今日播报"},
        ])

        dialogue_text = extract_final_ai_response([response])
        logger.info("broadcast_dialogue_generated", dialogue_length=len(dialogue_text))

        # Step 2: 调用火山引擎播客 API 生成双人语音
        audio_path = None
        try:
            podcast_service = get_podcast_service()
            audio_path = await podcast_service.generate_podcast(dialogue_text)
            logger.info("broadcast_audio_generated", audio_path=audio_path)
        except Exception as e:
            # 火山引擎失败不影响对话文本返回，记录错误后降级
            logger.error(
                "broadcast_tts_failed",
                error=str(e),
                exc_info=True,
            )

        # Step 3: 返回结果
        final_response = dialogue_text
        if audio_path:
            final_response += f"\n\n🎧 双人语音播报已生成：{audio_path}"

        return {
            "final_response": final_response,
            "dialogue_text": dialogue_text,
            "audio_path": audio_path,
        }

    except Exception as e:
        logger.error("agent_run_failed", agent="broadcast", error=str(e), exc_info=True)
        return {"final_response": "播报生成暂时不可用，请稍后重试"}