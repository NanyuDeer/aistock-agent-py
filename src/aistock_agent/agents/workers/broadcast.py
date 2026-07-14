"""播报 Agent — 双人对话播报生成

从数据库（scheduler 链路）或 state.analysis_reports（实时请求）集合各 Agent 分析结果，
生成 host + analyst 对话，并通过 Node.js 内部接口生成双人语音。
模型：deep_think（对话式播报生成）
"""

from langchain_core.messages import SystemMessage

from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.workers.broadcast import BROADCAST_ANALYST_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.message import extract_final_ai_response
from aistock_agent.utils.report_parser import extract_podcast_brief, extract_display_report

logger = get_logger(__name__)


async def _fetch_report_from_db(report_type: str, report_date: str) -> str | None:
    """从数据库读取分析报告的播报摘要

    优先读取 podcast_brief（schema_version 2.0 双层结构），
    如果没有则降级读取 display_report（兼容 1.0 单层结构）。

    Args:
        report_type: 报告类型 (morning/wind_leader/hot_burst)
        report_date: 报告日期 (YYYY-MM-DD)

    Returns:
        播报摘要文本，或 None（不存在）
    """
    data = await node_api.get_analysis_report(report_type, report_date)
    content = data.get("content") if data else None
    if not isinstance(content, dict):
        return None

    # 优先读取 podcast_brief（2.0 双层结构）
    brief = extract_podcast_brief(content)
    if brief:
        return brief

    # 降级读取 display_report（兼容 1.0 单层 text 字段）
    display = extract_display_report(content)
    if display:
        # 截取前 500 字作为降级播报材料，避免 token 过多
        return display[:500] if len(display) > 500 else display

    return None


async def run(state: AgentState) -> dict[str, object]:
    """播报生成：集合各 Agent 分析结果，生成双人对话播报内容

    流程：
    1. scheduler 触发时从数据库读取晨报、风口、机构调研报告；
       实时请求时从 state.analysis_reports 读取
    2. 调用 deep_think 生成双人对话文本
    3. scheduler 链路通过 Node.js 生成双人语音
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

        # Step 2: scheduler 链路先持久化文本，再由 Node.js 生成音频
        audio_path: str | None = None
        if state.get("trigger_source") == "scheduler" and report_date:
            try:
                saved = await node_api.save_analysis_report(
                    report_type="broadcast",
                    report_date=report_date,
                    content={"text": dialogue_text},
                )
                if saved is not None:
                    audio_data = await node_api.post(
                        "/internal/briefing/generate-audio",
                        {"date": report_date},
                        timeout=300.0,
                    )
                    raw_audio_path = audio_data.get("audio_path") if audio_data else None
                    if isinstance(raw_audio_path, str):
                        audio_path = raw_audio_path
                logger.info(
                    "broadcast_report_persisted",
                    report_date=report_date,
                    audio_generated=bool(audio_path),
                )
            except Exception as persist_err:
                logger.error("broadcast_persist_failed", error=str(persist_err))

        return {
            "final_response": dialogue_text,
            "dialogue_text": dialogue_text,
            "audio_path": audio_path,
        }
    except Exception as e:
        logger.error("agent_run_failed", agent="broadcast", error=str(e), exc_info=True)
        return {"final_response": "播报生成暂时不可用，请稍后重试"}
