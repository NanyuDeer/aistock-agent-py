"""P1：渠道B 印证结果在结论层（root）承载。"""
from aistock_agent.schemas.market_trace import MarketTraceResult


def _minimal_trace(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "1.1",
        "attribution_status": "hypothesis",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": [],
    }
    payload.update(overrides)
    return payload


def test_market_trace_result_defaults_confirmed_prediction_to_empty():
    trace = MarketTraceResult.model_validate(_minimal_trace())
    assert trace.confirmed_prediction == []


def test_market_trace_result_accepts_confirmed_prediction_at_root():
    trace = MarketTraceResult.model_validate(
        _minimal_trace(
            confirmed_prediction=[
                {
                    "prediction_id": "183",
                    "scenario": "量能萎缩后回踩",
                    "source_trace_id": "review:2026-10-06",
                    "confirmed_kind": "scene_match",
                    "confirmed_at": "2026-10-06T08:00:00+00:00",
                }
            ]
        )
    )
    assert len(trace.confirmed_prediction) == 1
    assert trace.confirmed_prediction[0].prediction_id == "183"


from aistock_agent.agents.workers.review import attach_confirmations_to_trace
from aistock_agent.trace.chain import PredictionConfirmation
from datetime import datetime, timezone


def _cnf(pid: str = "183") -> PredictionConfirmation:
    return PredictionConfirmation(
        prediction_id=pid,
        scenario="量能萎缩后回踩",
        source_trace_id="review:2026-10-06",
        confirmed_kind="scene_match",
        confirmed_at=datetime(2026, 10, 6, 8, 0, tzinfo=timezone.utc),
    )


def test_attach_writes_root_when_primary_chain_absent():
    """核心回归：primary_chain_id 为空也必须回填（旧实现此处直接 return False）。"""
    trace = MarketTraceResult.model_validate(_minimal_trace(primary_chain_id=None))
    ok = attach_confirmations_to_trace(trace, [_cnf()])
    assert ok is True
    assert len(trace.confirmed_prediction) == 1


def test_attach_returns_false_when_no_confirmations():
    trace = MarketTraceResult.model_validate(_minimal_trace())
    assert attach_confirmations_to_trace(trace, []) is False
