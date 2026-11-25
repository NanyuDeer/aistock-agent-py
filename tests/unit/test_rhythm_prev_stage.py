"""前一交易日主阶段提取（X2 / spec §5.12.2）：提取、越界拒绝、形状容错。"""
from aistock_agent.agents.workers.rhythm_master import _stage_from_report


def _resp(stage: object) -> dict[str, object]:
    return {
        "content": {
            "basis_date": "2026-09-18",
            "evidence": {"stage": stage, "stage_reason": "温度回落/量能转弱，退潮"},
        }
    }


def test_extracts_valid_stage() -> None:
    assert _stage_from_report(_resp("ebb")) == "ebb"
    assert _stage_from_report(_resp("rally")) == "rally"


def test_rejects_out_of_range_or_empty_stage() -> None:
    """越界野值必须拦在构造 RhythmEvidence 之前（否则 ValidationError 打断整轮）。"""
    assert _stage_from_report(_resp("boom")) is None
    assert _stage_from_report(_resp("")) is None
    assert _stage_from_report(_resp(None)) is None
    assert _stage_from_report(_resp(3)) is None


def test_rejects_malformed_response_shapes() -> None:
    assert _stage_from_report(None) is None
    assert _stage_from_report({}) is None
    assert _stage_from_report({"content": None}) is None
    assert _stage_from_report({"content": {"evidence": None}}) is None
    assert _stage_from_report({"content": {"evidence": {}}}) is None
