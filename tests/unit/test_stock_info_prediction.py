"""P2 Task 1：个股情报 → PredictionResult 确定性映射 + 入环门槛单测。

映射口径来源：docs/superpowers/specs/2026-10-06-双向印证闭环与个股情报改造-design.md §6.2/§6.3。
红线：conditions 恒空、horizons 恒 1 档、prediction_status 恒 hypothesis、summary 原文引用。
"""
from aistock_agent.services.stock_info_prediction import (
    REASON_BELOW_THRESHOLD,
    REASON_INVALID_INPUT,
    REASON_SAVED,
    REASON_UNMAPPED_VALUE,
    StockInfoPredictionRequest,
    build_stock_info_prediction,
    build_stock_info_prediction_with_reason,
    meets_entry_threshold,
    stock_info_source_id,
)


def _build_with_reason(**overrides: object):
    base: dict[str, object] = {
        "symbol": "300750",
        "stock_name": "宁德时代",
        "published_date": "2026-10-08",
        "ai_impact": "重大利好",
        "ai_horizon": "中期",
        "ai_summary": "公司获海外大额订单，预计中期业绩改善。",
        "url": "https://example.com/a",
    }
    base.update(overrides)
    return build_stock_info_prediction_with_reason(**base)  # type: ignore[arg-type]


def _build(**overrides: object):
    base: dict[str, object] = {
        "symbol": "300750",
        "stock_name": "宁德时代",
        "published_date": "2026-10-08",
        "ai_impact": "重大利好",
        "ai_horizon": "中期",
        "ai_summary": "公司获海外大额订单，预计中期业绩改善。",
        "url": "https://example.com/a",
    }
    base.update(overrides)
    return build_stock_info_prediction(**base)  # type: ignore[arg-type]


def test_source_id_format() -> None:
    assert stock_info_source_id("300750", "2026-10-08") == "stock_info:300750:2026-10-08"


def test_threshold_major_always_passes() -> None:
    assert meets_entry_threshold("重大利好", "短期") is True
    assert meets_entry_threshold("重大利空", "短期") is True


def test_threshold_normal_requires_long_horizon() -> None:
    assert meets_entry_threshold("利好", "短期") is False
    assert meets_entry_threshold("利好", "中期") is True
    assert meets_entry_threshold("利空", "中长期") is True
    assert meets_entry_threshold("利空", "长期") is True


def test_threshold_neutral_never_passes() -> None:
    assert meets_entry_threshold("中性", "长期") is False


def test_build_maps_impact_and_horizon() -> None:
    p = _build()
    assert p is not None
    assert p.schema_version == "3.0"
    assert p.prediction_status == "hypothesis"
    assert len(p.horizons) == 1
    h = p.horizons[0]
    assert h.horizon == "mid"
    assert h.direction == "bullish"
    assert h.target == "300750"
    assert h.phase == "building"
    assert h.remaining_estimate == "1-4 周"
    assert h.confidence == "low"
    assert h.confidence_source == "deterministic"
    assert "重大利好/中期" in h.metric_projection


def test_build_no_conditions_and_summary_verbatim() -> None:
    p = _build(ai_summary="原文照抄的一句话结论。")
    assert p is not None
    assert p.conditions == []
    assert p.risks == []
    assert p.evolution_steps == []
    assert p.evolution_narrative == "原文照抄的一句话结论。"
    assert p.attribution_summary == "原文照抄的一句话结论。"


def test_build_evidence_ids_non_empty_and_traceable() -> None:
    p = _build()
    assert p is not None
    assert "stock_info:300750:2026-10-08" in p.evidence_ids
    assert "https://example.com/a" in p.evidence_ids


def test_build_target_uses_bare_code_as_internal_id() -> None:
    p = _build(symbol="000001", stock_name="平安银行")
    assert p is not None
    assert p.target is not None
    assert p.target.kind == "stock"
    assert p.target.internal_id == "000001"
    assert p.target.code == "000001.SZ"
    assert p.target.name == "平安银行"
    assert p.extraction_source == "stock_info_judge"


def test_build_returns_none_when_threshold_not_met() -> None:
    assert _build(ai_impact="利好", ai_horizon="短期") is None
    assert _build(ai_impact="中性", ai_horizon="长期") is None


def test_build_returns_none_on_invalid_symbol_or_date() -> None:
    assert _build(symbol="ABC") is None
    assert _build(published_date="2026/10/08") is None


def test_exchange_suffix_rules() -> None:
    assert _build(symbol="600519").target.code == "600519.SH"  # type: ignore[union-attr]
    assert _build(symbol="300750").target.code == "300750.SZ"  # type: ignore[union-attr]
    assert _build(symbol="000001").target.code == "000001.SZ"  # type: ignore[union-attr]
    assert _build(symbol="830799").target.code == "830799.BJ"  # type: ignore[union-attr]


def test_request_model_rejects_extra_key() -> None:
    import pydantic
    import pytest

    with pytest.raises(pydantic.ValidationError):
        StockInfoPredictionRequest(
            symbol="300750", stock_name="宁德时代", published_date="2026-10-08",
            ai_impact="利好", ai_horizon="中期", ai_summary="x", url=None, extra_key=1,
        )


# ── 终审 #2：build_..._with_reason 须区分三类 skipped 与 saved（app-api 据此分诊） ──


def test_build_with_reason_saved() -> None:
    p, code = _build_with_reason()
    assert p is not None
    assert code == REASON_SAVED


def test_build_with_reason_below_threshold() -> None:
    p, code = _build_with_reason(ai_impact="利好", ai_horizon="短期")
    assert p is None
    assert code == REASON_BELOW_THRESHOLD
    # 中性同样属门槛未达（预期正常降级）
    _, code_neutral = _build_with_reason(ai_impact="中性", ai_horizon="长期")
    assert code_neutral == REASON_BELOW_THRESHOLD


def test_build_with_reason_invalid_input() -> None:
    p, code = _build_with_reason(symbol="ABC")
    assert p is None
    assert code == REASON_INVALID_INPUT
    _, code_date = _build_with_reason(published_date="2026/10/08")
    assert code_date == REASON_INVALID_INPUT


def test_build_with_reason_unmapped_value() -> None:
    # 前缀 99 不在交易所映射表 → 映射缺档（门槛已过）
    p, code = _build_with_reason(symbol="999999")
    assert p is None
    assert code == REASON_UNMAPPED_VALUE


def test_build_stock_info_prediction_wrapper_unchanged() -> None:
    """薄封装须与 with_reason 同口径：成功仍返回 PredictionResult，失败仍 None。"""
    assert build_stock_info_prediction(
        symbol="300750", stock_name="宁德时代", published_date="2026-10-08",
        ai_impact="重大利好", ai_horizon="中期", ai_summary="x", url=None,
    ) is not None
    assert build_stock_info_prediction(
        symbol="300750", stock_name="宁德时代", published_date="2026-10-08",
        ai_impact="利好", ai_horizon="短期", ai_summary="x", url=None,
    ) is None
