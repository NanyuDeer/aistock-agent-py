"""event_analysis_pipeline 单元测试（P0-2：单事件超时透传 + GI 接线）"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_analysis_pipeline import run_event_analysis_pipeline
from aistock_agent.services.event_conduction import (
    AnalysisReportPayload,
    EventConductionOutput,
    EventConductionResult,
)

_MODULE = "aistock_agent.services.event_analysis_pipeline"


def _payload(event_id: str) -> AnalysisReportPayload:
    return AnalysisReportPayload(
        event_id=event_id,
        summary=f"摘要-{event_id}",
        original_event=f"请分析以下重大事件：事件-{event_id}",
        impact_industries=["半导体"],
        impact_chain=[],
        key_variables=[],
        mechanism="传导机制",
        investment_rating="positive",
        investment_conclusion="投资结论",
    )


def _success_output(event_id: str) -> EventConductionOutput:
    return EventConductionOutput(
        status=EventConductionResult(
            success=True,
            event_id=event_id,
            title=f"事件-{event_id}",
            event_generated=True,
            persisted=True,
            error=None,
        ),
        analysis_report=_payload(event_id),
    )


def _failed_output(event_id: str) -> EventConductionOutput:
    return EventConductionOutput(
        status=EventConductionResult(
            success=False,
            event_id=event_id,
            title=f"失败-{event_id}",
            event_generated=False,
            persisted=False,
            error="persist failed",
            error_type="persist_failed",
        ),
        analysis_report=None,
    )


@pytest.mark.asyncio
async def test_pipeline_passes_per_event_timeout_and_runs_gi() -> None:
    """P0-2：pipeline 将配置的超时作为 per_event_timeout 传给 batch，
    成功事件进入 GI，失败事件被 _to_gi_events 排除。"""
    events = [
        {"title": "事件A", "summary": "摘要A"},
        {"title": "事件B", "summary": "摘要B"},
    ]

    async def fake_batch(major_events, *, per_event_timeout=None):
        assert per_event_timeout == 900
        return [_success_output("evt_a"), _failed_output("evt_b")]

    with (
        patch(f"{_MODULE}.run_event_conduction_batch", side_effect=fake_batch),
        patch(
            "aistock_agent.services.global_importance_evaluation.persist_global_importance_evaluation",
            new_callable=AsyncMock,
            return_value={"top_bullish_event": {"event_id": "evt_a"}, "persisted": True},
        ) as mock_gi,
    ):
        result = await run_event_analysis_pipeline(events)

    assert result["event_count"] == 2
    assert result["timed_out"] is False
    assert result["error"] is None
    # 只有 success=True 的事件进入 GI（evt_a），失败事件被排除
    gi_events = mock_gi.call_args.args[0]
    assert [e["event_id"] for e in gi_events] == ["evt_a"]
    assert result["gi_result"]["persisted"] is True
