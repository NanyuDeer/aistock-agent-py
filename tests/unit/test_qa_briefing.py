"""固定日期 QA Brief 编排：只能消费已审核、可追溯的上游报告。"""

from unittest.mock import AsyncMock

import pytest

from aistock_agent.services.qa_briefing import (
    QaBriefingPrerequisiteError,
    run_qa_brief_chain,
)


def _report(report_type: str, report_id: int) -> dict[str, object]:
    return {
        "id": report_id,
        "report_type": report_type,
        "report_date": "2026-07-24",
        "status": "completed",
        "data_source": f"audited_{report_type}",
        "created_at": "2026-07-24T09:00:00+08:00",
        "content": {
            "schema_version": "2.0",
            "display_report": {"summary": f"{report_type} 审核结论"},
            "podcast_brief": f"{report_type} 审核播报结论",
        },
    }


@pytest.mark.asyncio
async def test_qa_runner_rejects_missing_audited_upstream_before_persisting_or_tts() -> None:
    api = AsyncMock()
    api.get_analysis_report.side_effect = lambda report_type, _date: (
        _report("morning", 11) if report_type == "morning" else None
    )
    api.list_analysis_reports.return_value = []
    broadcast_runner = AsyncMock()

    with pytest.raises(QaBriefingPrerequisiteError, match="wind_leader.*hot_burst"):
        await run_qa_brief_chain(
            "morning",
            "2026-07-24",
            "run-20260724-a",
            api=api,
            broadcast_runner=broadcast_runner,
        )

    api.save_analysis_report.assert_not_awaited()
    broadcast_runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_qa_runner_persists_complete_fixed_date_brief_then_generates_audio() -> None:
    api = AsyncMock()
    reports = {
        "morning": _report("morning", 11),
        "wind_leader": _report("wind_leader", 12),
        "hot_burst": _report("hot_burst", 13),
    }
    api.get_analysis_report.side_effect = lambda report_type, _date: reports.get(report_type)
    api.list_analysis_reports.return_value = []
    api.save_analysis_report.return_value = {"id": 100}
    broadcast_runner = AsyncMock(return_value={
        "final_response": "[]",
        "audio_path": "/api/agent/audio/broadcast-morning-2026-07-24.mp3",
    })

    result = await run_qa_brief_chain(
        "morning",
        "2026-07-24",
        "run-20260724-a",
        api=api,
        broadcast_runner=broadcast_runner,
    )

    assert result == {
        "success": True,
        "run_id": "run-20260724-a",
        "brief_type": "morning",
        "report_date": "2026-07-24",
        "audio_path": "/api/agent/audio/broadcast-morning-2026-07-24.mp3",
    }
    saved = api.save_analysis_report.await_args.kwargs
    assert saved["report_type"] == "brief_morning"
    assert saved["report_date"] == "2026-07-24"
    assert saved["data_source"] == "brief_aggregator"
    assert saved["status"] == "completed"
    assert saved["content"]["degraded"] is False
    state = broadcast_runner.await_args.args[0]
    assert state["trigger_source"] == "scheduler"
    assert state["report_date"] == "2026-07-24"
    assert state["brief_type"] == "morning"
    assert state["session_id"] == "qa_run-20260724-a_morning_2026-07-24"
