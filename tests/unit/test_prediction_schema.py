import pytest
from pydantic import ValidationError

from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult, PredictionRisk


def _valid_horizon(**overrides):
    data = {
        "horizon": "mid",
        "remaining_estimate": "2-4 周",
        "phase": "peaking",
        "direction": "bullish",
        "target": "上证指数",
        "metric_projection": "上证指数维持 3500-3600 区间",
        "confidence": "medium",
    }
    data.update(overrides)
    return data


def test_valid_result():
    result = PredictionResult(
        schema_version="1.0",
        prediction_status="confirmed",
        horizons=[PredictionHorizon(**_valid_horizon())],
        evolution_narrative="短线已兑现大半，中线延续，长线衰减",
        risks=[PredictionRisk(factor="政策转向", invalidation="宽松转向收紧则失效")],
        evidence_ids=["src-1"],
        attribution_summary="政策利好传导 2-4 周，板块轮动延续",
    )
    assert result.horizons[0].horizon == "mid"


def test_empty_horizons_raises():
    with pytest.raises(ValidationError):
        PredictionResult(
            schema_version="1.0",
            prediction_status="confirmed",
            horizons=[],
            evolution_narrative="x",
            risks=[],
            evidence_ids=[],
        )


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        PredictionResult(
            schema_version="1.0",
            prediction_status="confirmed",
            horizons=[PredictionHorizon(**_valid_horizon())],
            evolution_narrative="x",
            risks=[],
            evidence_ids=[],
            unknown_field="x",
        )


def test_invalid_horizon_literal():
    with pytest.raises(ValidationError):
        PredictionHorizon(**_valid_horizon(horizon="week"))
