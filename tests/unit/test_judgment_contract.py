"""研判契约 5 条硬约束。"""

import pytest
from pydantic import ValidationError

from aistock_agent.schemas.judgment import Judgment


def _evidence(evidence_id: str, source: str = "internal:index_kline") -> dict:
    return {
        "evidence_id": evidence_id,
        "kind": "market",
        "subject": "上证指数",
        "claim": f"证据 {evidence_id}",
        "source": source,
        "source_type": "market_data",
        "published_at": "2026-11-25",
        "fetched_at": "2026-11-25T07:00:00+00:00",
        "quality": "high",
        "weight": 1.0,
        "value": 1.23,
        "unit": "%",
    }


def _payload(**overrides) -> dict:
    payload = {
        "schema_version": "judgment.v1",
        "judgment_id": "jdg_20261125_000001",
        "subject": "上证指数",
        "subject_code": "000001",
        "subject_type": "market",
        "horizon": "short",
        "as_of": "2026-11-25",
        "due_date": "2026-12-02",
        "direction": "bullish",
        "confidence": 0.62,
        "thesis": "缩量企稳后维持震荡走强",
        "evidence": [_evidence("ev_1"), _evidence("ev_2", source="internal:market_breadth")],
        "counter_evidence": [_evidence("ev_3")],
        "conflicts": [],
        "invalidators": ["收盘跌破近 5 日最低点"],
        "verify_at": "2026-12-07",
        "modules": ["market_perception"],
        "render": {
            "home": True, "stock_page": False, "event_page": False,
            "broadcast": True, "push": True, "chat": True, "pick_page": False,
        },
    }
    payload.update(overrides)
    return payload


def test_valid_judgment_passes():
    judgment = Judgment.model_validate(_payload())
    assert judgment.judgment_id == "jdg_20261125_000001"
    assert judgment.confidence == 0.62
    assert judgment.verified_result is None


def test_bad_judgment_id_rejected():
    with pytest.raises(ValidationError):
        Judgment.model_validate(_payload(judgment_id="jdg-20261125-000001"))


def test_single_source_high_confidence_is_downgraded():
    """硬约束 #2：confidence >= 0.7 必须 >= 2 个独立 source，否则降到 0.69 并记冲突。"""
    payload = _payload(confidence=0.8, evidence=[_evidence("ev_1")])
    judgment = Judgment.model_validate(payload)
    assert judgment.confidence == 0.69
    assert len(judgment.conflicts) == 1
    assert "single_source" in judgment.conflicts[0].conflict_id


def test_missing_counter_evidence_rejected():
    payload = _payload()
    payload.pop("counter_evidence")
    with pytest.raises(ValidationError):
        Judgment.model_validate(payload)


def test_verify_at_must_be_due_plus_3_trading_days():
    """硬约束 #4：verify_at = due_date + 3 个交易日（2026-12-02 → 2026-12-07）。"""
    judgment = Judgment.model_validate(_payload(verify_at="2026-12-07"))
    assert judgment.verify_at == "2026-12-07"
    with pytest.raises(ValidationError):
        Judgment.model_validate(_payload(verify_at="2026-12-04"))


def test_render_requires_all_seven_targets():
    """硬约束 #5：7 个出口键必须齐全，缺一即失败（禁止隐式默认）。"""
    render = {
        "home": True, "stock_page": False, "event_page": False,
        "broadcast": True, "push": True, "chat": True,
    }
    with pytest.raises(ValidationError):
        Judgment.model_validate(_payload(render=render))
