from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.review import _build_review_report, _persist_prediction_record
from aistock_agent.schemas.market_trace import ReviewArtifact
from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult, PredictionRisk


def _artifact(with_prediction: bool) -> ReviewArtifact:
    snapshot = {
        "snapshot_id": "snap-1",
        "trade_date": "2026-08-10",
        "captured_at": "2026-08-10T00:00:00Z",
        "a_share": {"indices": [], "sectors": {}},
        "sources": {},
        "missing_fields": [],
        "phenomenon_discovery": {
            "status": "no_phenomenon",
            "primary": None,
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "complete",
                "attribution_inputs": "complete",
                "causal_evidence": "not_ready",
            },
            "diagnostics": [],
        },
    }
    trace = {
        "schema_version": "1.1",
        "attribution_status": "not_applicable",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": ["无需归因分析"],
    }
    prediction = (
        PredictionResult(
            schema_version="1.0",
            prediction_status="hypothesis",
            horizons=[PredictionHorizon(horizon="mid", remaining_estimate="2-4 周", phase="peaking",
                                        direction="bullish", target="上证指数",
                                        metric_projection="上证指数区间上移", confidence="medium")],
            evolution_narrative="中线延续",
            risks=[PredictionRisk(factor="政策转向", invalidation="宽松转紧失效")],
            evidence_ids=[],
        )
        if with_prediction
        else None
    )
    return ReviewArtifact(
        schema_version="1.1",
        snapshot=snapshot,
        trace=trace,
        markdown="# A股收盘溯源",
        trace_summary="无",
        sectors=[],
        prediction=prediction,
    )


def test_artifact_prediction_roundtrip():
    artifact = _artifact(with_prediction=True)
    dumped = artifact.model_dump(mode="json")
    restored = ReviewArtifact.model_validate(dumped)
    assert restored.prediction is not None
    assert restored.prediction.horizons[0].horizon == "mid"


def test_artifact_prediction_defaults_none():
    artifact = _artifact(with_prediction=False)
    assert artifact.prediction is None


def test_build_review_report_includes_prediction():
    content = _build_review_report(_artifact(with_prediction=True))
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    assert market_trace["prediction"]["prediction_status"] == "hypothesis"


def test_build_review_report_prediction_none():
    content = _build_review_report(_artifact(with_prediction=False))
    market_trace = content["market_trace"]
    assert isinstance(market_trace, dict)
    assert market_trace["prediction"] is None


@pytest.mark.asyncio
async def test_persist_prediction_record_saves_when_scheduler():
    state = {"trigger_source": "scheduler", "report_date": "2026-08-10"}
    run_result = AsyncMock()
    run_result.prediction.schema_version = "1.0"
    run_result.prediction.model_dump.return_value = {"schema_version": "1.0"}
    run_result.due_dates = {"mid": "2026-09-07"}
    with patch(
        "aistock_agent.agents.workers.review.node_api.save_prediction",
        new=AsyncMock(return_value={"id": 1}),
    ) as save:
        await _persist_prediction_record(state, run_result)
    save.assert_awaited_once()
    payload = save.await_args.args[0]
    assert payload["source_type"] == "market_trace"
    assert payload["source_id"] == "review:2026-08-10"
    assert payload["due_dates"] == {"mid": "2026-09-07"}
