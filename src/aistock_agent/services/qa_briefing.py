"""仅供隔离 QA 使用的固定日期 Brief 与播报编排。"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from aistock_agent.services.briefing import BriefingClient, BriefType, build_brief
from aistock_agent.services.data_client import node_api
from aistock_agent.state.schema import AgentState

BroadcastRunner = Callable[[AgentState], Awaitable[dict[str, object]]]


class QaBriefingPrerequisiteError(ValueError):
    """指定日期的审核上游报告不完整或不可追溯。"""


class QaBriefingRunError(RuntimeError):
    """QA Brief 已校验上游，但持久化、播报或音频未完成。"""


async def run_qa_brief_chain(
    brief_type: BriefType,
    report_date: str,
    run_id: str,
    *,
    api: BriefingClient = node_api,
    broadcast_runner: BroadcastRunner | None = None,
) -> dict[str, object]:
    """对固定日期已落库的真实工件执行 Brief → Broadcast → Audio。

    本函数不触发任何上游抓取、历史回放或样本写入；Brief 的完整性由
    ``build_brief`` 对真实报告 ID、状态、来源、时间与受控结论逐项校验。
    """
    if brief_type not in ("morning", "evening"):
        raise ValueError("brief_type 必须是 morning 或 evening")
    if not run_id.strip():
        raise ValueError("run_id 不能为空")

    brief = await build_brief(brief_type, report_date, api=api)
    missing_sources = brief.get("missing_sources")
    if brief.get("degraded") is True or not isinstance(missing_sources, list) or missing_sources:
        missing = ""
        if isinstance(missing_sources, list):
            missing = ", ".join(str(item) for item in missing_sources)
        raise QaBriefingPrerequisiteError(f"指定日期缺少可追溯上游报告: {missing}")

    saved = await api.save_analysis_report(
        report_type=f"brief_{brief_type}",
        report_date=report_date,
        content=brief,
        data_source="brief_aggregator",
        status="completed",
    )
    if saved is None:
        raise QaBriefingRunError("QA Brief 持久化失败")

    runner = broadcast_runner
    if runner is None:
        from aistock_agent.agents.workers.broadcast import run as internal_broadcast_runner

        runner = internal_broadcast_runner

    state: AgentState = {
        "messages": [],
        "session_id": f"qa_{run_id}_{brief_type}_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        # 主线播报仅在既有 scheduler 分支中持久化并生成音频。
        "trigger_source": "scheduler",
        "report_date": report_date,
        "brief_type": brief_type,
    }
    broadcast = await runner(state)
    audio_path = broadcast.get("audio_path")
    if not isinstance(audio_path, str) or not audio_path:
        raise QaBriefingRunError("QA 播报或音频生成失败")

    return {
        "success": True,
        "run_id": run_id,
        "brief_type": brief_type,
        "report_date": report_date,
        "audio_path": audio_path,
    }
