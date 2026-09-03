import pytest
from pydantic import ValidationError

from aistock_agent.schemas.prediction import (
    PredictionAnchor,
    PredictionCondition,
    PredictionHorizon,
    PredictionResult,
    PredictionRisk,
)
from aistock_agent.schemas.target import Target


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
        schema_version="3.0",
        prediction_status="confirmed",
        horizons=[PredictionHorizon(**_valid_horizon())],
        evolution_narrative="短线已兑现大半，中线延续，长线衰减",
        risks=[PredictionRisk(factor="政策转向", invalidation="宽松转向收紧则失效")],
        evidence_ids=["src-1"],
        attribution_summary="政策利好传导 2-4 周，板块轮动延续",
    )
    assert result.horizons[0].horizon == "mid"


def test_labels_default_empty_and_parse():
    """2026-09-03 label 展示字段：horizon.label（基准走势）与 condition.label（两段式路径名）。
    新数据携带 label 正常解析；旧记录缺省为空字符串，不破坏既有校验。"""
    horizon_with = PredictionHorizon(**_valid_horizon(label="恐慌出清为主"))
    assert horizon_with.label == "恐慌出清为主"
    horizon_old = PredictionHorizon(**_valid_horizon())
    assert horizon_old.label == ""

    cond_with = PredictionCondition(
        condition="成交额放大至 900 亿以上、收盘较当前再跌超 2%",
        label="恐慌出清 · 下跌中继",
        scenario="恐慌出清、惯性下探 -3%~-5%",
        anchor=PredictionAnchor(horizon="short", threshold="-3%", direction="bearish"),
    )
    assert cond_with.label == "恐慌出清 · 下跌中继"
    cond_old = PredictionCondition(
        condition="缩量企稳、不破前低",
        scenario="空头衰竭、修复至平台",
        anchor=PredictionAnchor(horizon="short", threshold="+5%", direction="bullish"),
    )
    assert cond_old.label == ""


def test_empty_horizons_raises():
    with pytest.raises(ValidationError):
        PredictionResult(
            schema_version="3.0",
            prediction_status="confirmed",
            horizons=[],
            evolution_narrative="x",
            risks=[],
            evidence_ids=[],
        )


def test_extra_field_rejected():
    with pytest.raises(ValidationError):
        PredictionResult(
            schema_version="3.0",
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


# ===== 条件化预判 schema 层（P1 / Spec A §3.1）=====


def test_prediction_anchor_defaults_metric_close():
    """anchor.metric 缺省 close。"""
    a = PredictionAnchor(horizon="short", threshold="+5%", direction="bullish")
    assert a.metric == "close"
    assert a.direction == "bullish"


def test_prediction_anchor_direction_defaults_neutral():
    """direction 缺省 neutral（Spec A §4.1 决策：schema 放行，归一化层从文本兜底）。"""
    a = PredictionAnchor(horizon="short", threshold="+5%", metric="close")
    assert a.direction == "neutral"


def test_prediction_anchor_direction_literal():
    """direction 必须是 bullish/bearish/neutral 之一。"""
    with pytest.raises(ValidationError):
        PredictionAnchor(horizon="short", threshold="+5%", direction="sideways")  # type: ignore[arg-type]


def test_prediction_anchor_metric_literal():
    """metric 限定 close/high/low/volume/index_close。"""
    with pytest.raises(ValidationError):
        PredictionAnchor(horizon="short", threshold="+5%", metric="open")  # type: ignore[arg-type]


def test_prediction_condition_full():
    """condition/scenario/anchor 三段齐全。"""
    c = PredictionCondition(
        condition="若明日放量站稳前高 82.50 元",
        scenario="则趋势延续，上看 +5%",
        anchor=PredictionAnchor(
            horizon="short", threshold="+5%", metric="close", direction="bullish"
        ),
    )
    assert c.anchor.horizon == "short"
    assert c.anchor.direction == "bullish"


def test_prediction_condition_rejects_extra_field():
    with pytest.raises(ValidationError):
        PredictionCondition(
            condition="x",
            scenario="y",
            anchor={"horizon": "short", "threshold": "+1%", "direction": "neutral"},
            unknown_field="x",  # type: ignore[call-arg]
        )


def test_prediction_result_conditions_able():
    """PredictionResult.conditions 为可选字段（旧 2.0 记录为空），schema_version 升 3.0。"""
    result = PredictionResult(
        schema_version="3.0",
        prediction_status="confirmed",
        horizons=[PredictionHorizon(**_valid_horizon())],
        conditions=[
            PredictionCondition(
                condition="若放量站上前高",
                scenario="则看多 +5%",
                anchor={"horizon": "short", "threshold": "+5%", "direction": "bullish"},
            )
        ],
        evolution_narrative="x",
        risks=[],
        evidence_ids=[],
    )
    assert len(result.conditions) == 1
    assert result.conditions[0].anchor.direction == "bullish"


def test_prediction_result_conditions_default_empty():
    """conditions 缺省为空（兼容存量无 conditions 产出）。"""
    result = PredictionResult(
        schema_version="3.0",
        prediction_status="confirmed",
        horizons=[PredictionHorizon(**_valid_horizon())],
        evolution_narrative="x",
        risks=[],
        evidence_ids=[],
    )
    assert result.conditions == []
    assert result.target is None


def test_prediction_result_target_association():
    """PredictionResult.target 关联首类 Target 对象（全局 §2.1 数据卫生：internal_id 带后缀）。"""
    stock = Target(kind="stock", internal_id="600519.SH", code="600519.SH", name="贵州茅台")
    result = PredictionResult(
        schema_version="3.0",
        prediction_status="confirmed",
        horizons=[PredictionHorizon(**_valid_horizon())],
        target=stock,
        evolution_narrative="x",
        risks=[],
        evidence_ids=[],
    )
    assert result.target is not None
    assert result.target.internal_id == "600519.SH"


def test_prediction_schema_version_literal_3():
    """schema_version 字面量升为 3.0，拒绝旧 2.0。"""
    with pytest.raises(ValidationError):
        PredictionResult(
            schema_version="2.0",
            prediction_status="confirmed",
            horizons=[PredictionHorizon(**_valid_horizon())],
            evolution_narrative="x",
            risks=[],
            evidence_ids=[],
        )


# ===== omitted_horizons 缺档留痕（Task 2 / spec §5.3）=====


def _base_result(horizons: list[str]) -> PredictionResult:
    """最小合法 PredictionResult（字段见 schemas/prediction.py，horizons 按输入档位列表构造）。"""
    return PredictionResult(
        schema_version="3.0",
        prediction_status="confirmed",
        horizons=[
            {
                "horizon": h, "remaining_estimate": "2-4 周", "phase": "building",
                "direction": "bullish", "target": "上证指数", "metric_projection": "+2%",
                "confidence": "medium",
            }
            for h in horizons
        ],
        evolution_narrative="短期冲高后回落",
        risks=[{"factor": "政策转向", "invalidation": "若出现收紧"}],
        evidence_ids=["evt-1"],
    )


def test_omitted_horizons_roundtrip():
    r = PredictionResult(**{**_base_result(["short"]).model_dump(),
                            "omitted_horizons": [
                                {"horizon": "mid", "reason": "情绪性脉冲，缺乏中期产业逻辑"},
                                {"horizon": "long", "reason": "无中长期催化"},
                            ]})
    assert [o.horizon for o in r.omitted_horizons] == ["mid", "long"]
    assert r.schema_version == "3.0"


def test_omitted_horizons_reject_overlap_with_horizons():
    with pytest.raises(ValueError):
        PredictionResult(**{**_base_result(["short", "mid"]).model_dump(),
                            "omitted_horizons": [{"horizon": "mid", "reason": "x"}]})


def test_omitted_horizons_reject_blank_reason():
    # spec §5.4 归一化层校验非空：空白/纯空格 reason 无解释价值，校验层拒绝（final fix）
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(ValidationError):
            PredictionResult(**{**_base_result(["short"]).model_dump(),
                                "omitted_horizons": [{"horizon": "mid", "reason": blank}]})
