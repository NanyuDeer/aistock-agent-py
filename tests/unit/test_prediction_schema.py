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


def test_horizon_and_condition_label_default_empty_on_legacy_record():
    """旧记录（已落库 JSON 无 label 键）反序列化 → label 空串，不 break 校验（spec §3）。"""
    horizon = PredictionHorizon.model_validate(_valid_horizon())
    assert horizon.label == ""

    cond = PredictionCondition.model_validate(
        {
            "condition": "缩量企稳、不破前低",
            "scenario": "空头衰竭、修复至平台",
            "anchor": {"horizon": "short", "threshold": "+5%", "direction": "bullish"},
        }
    )
    assert cond.label == ""

    # 默认值声明在字段层（而非仅构造期产物）——防回归：label 不得变为必填或被改成 None
    assert PredictionHorizon.model_fields["label"].default == ""
    assert PredictionCondition.model_fields["label"].default == ""


def test_horizon_and_condition_label_parse_and_roundtrip():
    """label 显式传入 → 原样解析（不裁剪/不改写），并随 model_dump 序列化往返保持。"""
    horizon = PredictionHorizon(**_valid_horizon(label="恐慌出清为主"))
    cond = PredictionCondition(
        condition="成交额放大至 900 亿以上、收盘较当前再跌超 2%",
        label="恐慌出清 · 下跌中继",
        scenario="恐慌出清、惯性下探 -3%~-5%",
        anchor=PredictionAnchor(horizon="short", threshold="-3%", direction="bearish"),
    )
    assert horizon.label == "恐慌出清为主"
    assert cond.label == "恐慌出清 · 下跌中继"
    assert PredictionHorizon.model_validate(horizon.model_dump()).label == "恐慌出清为主"
    assert PredictionCondition.model_validate(cond.model_dump()).label == "恐慌出清 · 下跌中继"


def test_scenario_keywords_default_empty_and_parse():
    """2026-09-03 scenario_keywords 预判关键词：与 condition keywords 同构（1~2 个/≤10 字），
    新数据携带正常解析、旧记录缺省为空数组。"""
    cond_with = PredictionCondition(
        condition="若放量站稳前高",
        label="放量突破 · 短线续攻",
        keywords=["量能≥2800亿"],
        scenario_keywords=["上探+3%~+5%", "分歧加大"],
        scenario="短线做多动能延续，累计涨幅上看 +3%~+5%，之后分歧加大。",
        anchor=PredictionAnchor(horizon="short", threshold="+3%", direction="bullish"),
    )
    assert cond_with.scenario_keywords == ["上探+3%~+5%", "分歧加大"]
    cond_old = PredictionCondition(
        condition="缩量企稳、不破前低",
        scenario="空头衰竭、修复至平台",
        anchor=PredictionAnchor(horizon="short", threshold="+5%", direction="bullish"),
    )
    assert cond_old.scenario_keywords == []


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


# ===== anchor 判定维度扩展：metric 枚举 + op/level（Task 5.1 / spec §12.3）=====


@pytest.mark.parametrize("metric", [
    "close", "high", "low", "volume", "index_close",          # 存量
    "amount", "ma20", "ma60", "prior_low", "prior_high",       # 量类 / 技术位
    "today_open", "today_high", "today_low",                   # 参考位
])
def test_prediction_anchor_metric_extended_enum(metric: str) -> None:
    """metric 枚举扩展：新增 amount/均线/前低新高/参考位 全部可解析（含存量值不回归）。"""
    a = PredictionAnchor(horizon="short", threshold="+5%", metric=metric)  # type: ignore[arg-type]
    assert a.metric == metric


def test_prediction_anchor_metric_still_rejects_unknown() -> None:
    """未登记值（open/turnover）仍被 Literal 拒绝——枚举是白名单，不是自由字符串。"""
    for bad in ("open", "turnover", "ma5"):
        with pytest.raises(ValidationError):
            PredictionAnchor(horizon="short", threshold="+5%", metric=bad)  # type: ignore[arg-type]


def test_prediction_anchor_op_level_default_none() -> None:
    """op/level 为可选字段（带默认值）→ 旧记录反序列化缺省 None，不升 schema_version。"""
    a = PredictionAnchor.model_validate(
        {"horizon": "short", "threshold": "+5%", "direction": "bullish"}
    )
    assert a.op is None
    assert a.level is None
    # 默认值声明在字段层——防回归：不得变为必填
    assert PredictionAnchor.model_fields["op"].default is None
    assert PredictionAnchor.model_fields["level"].default is None


@pytest.mark.parametrize("op", ["gte", "lte", "above", "below", "cross_above", "cross_below"])
def test_prediction_anchor_op_enum(op: str) -> None:
    a = PredictionAnchor(
        horizon="short", threshold="+5%", metric="volume", op=op, level=2.2e8  # type: ignore[arg-type]
    )
    assert a.op == op
    assert a.level == 2.2e8


def test_prediction_anchor_rejects_unknown_op() -> None:
    """op 白名单外（gt/ge/cross）整条拒绝（判定层只认这 6 个原子操作）。"""
    for bad in ("gt", "ge", "cross", "breakout"):
        with pytest.raises(ValidationError):
            PredictionAnchor(
                horizon="short", threshold="+5%", metric="volume", op=bad  # type: ignore[arg-type]
            )


def test_prediction_condition_anchor_op_level_roundtrip() -> None:
    """量类条件全链（dict 输入 json_mode 路径 + 序列化往返）保留 op/level。"""
    cond = PredictionCondition.model_validate(
        {
            "condition": "板块放量至 1.2 亿手以上",
            "scenario": "量能确认后短线续攻 +3%",
            "anchor": {
                "horizon": "short",
                "threshold": "+3%",
                "metric": "volume",
                "direction": "bullish",
                "op": "gte",
                "level": 120000000.0,
            },
        }
    )
    assert cond.anchor.op == "gte"
    assert cond.anchor.level == 120000000.0
    again = PredictionCondition.model_validate(cond.model_dump())
    assert (again.anchor.op, again.anchor.level) == ("gte", 120000000.0)


def test_prediction_anchor_rejects_unknown_key_around_op_level() -> None:
    """extra=forbid 未放开：op/level 的近义拼写仍整条拒绝——证明 prompt 键清单须与 schema
    同批同步，否则 LLM 多吐一个键即整条预判丢失（parse_failed / None）。"""
    for bad in ("operator", "levels", "level_pct", "condition_type"):
        with pytest.raises(ValidationError):
            PredictionAnchor.model_validate(
                {"horizon": "short", "threshold": "+5%", "direction": "bullish", bad: 1.0}
            )


# ===== anchor.event_ref：事件类条件的锚（Task 0.3 / spec §13.2）=====


def test_prediction_anchor_accepts_event_ref():
    """事件类条件的锚：显式传 event_ref → 原样解析（指向 Event Entity 的 event_id）。"""
    a = PredictionAnchor(
        horizon="mid", threshold="+5%", direction="bullish", event_ref="evt_20260917_001"
    )
    assert a.event_ref == "evt_20260917_001"


def test_prediction_anchor_event_ref_default_none_on_legacy_record():
    """旧记录（anchor 无 event_ref 键）反序列化 → None，不 break 校验（不升 schema_version）。"""
    a = PredictionAnchor.model_validate(
        {"horizon": "short", "threshold": "+5%", "direction": "bullish"}
    )
    assert a.event_ref is None
    # 默认值声明在字段层——防回归：event_ref 不得变为必填
    assert PredictionAnchor.model_fields["event_ref"].default is None


def test_prediction_condition_anchor_event_ref_roundtrip():
    """条件→情景→anchor 全链带 event_ref：dict 输入（json_mode 路径）与序列化往返均保留。"""
    cond = PredictionCondition.model_validate(
        {
            "condition": "若出口限制细则落地且相关个股放量下探",
            "scenario": "情绪转弱，短线回踩 -3% 内",
            "anchor": {
                "horizon": "short",
                "threshold": "-3%",
                "metric": "close",
                "direction": "bearish",
                "event_ref": "evt_20260917_001",
            },
        }
    )
    assert cond.anchor.event_ref == "evt_20260917_001"
    assert (
        PredictionCondition.model_validate(cond.model_dump()).anchor.event_ref
        == "evt_20260917_001"
    )


def test_prediction_anchor_rejects_unknown_key_around_event_ref():
    """extra=forbid 未放开：拼错的键（eventrefs/eventRef）仍整条拒绝——该 guard 证明 prompt
    键清单必须与 schema 同批同步，否则 LLM 多吐一个键即整条预判丢失（parse_failed / None）。"""
    for bad in ("eventrefs", "eventRef"):
        with pytest.raises(ValidationError):
            PredictionAnchor.model_validate(
                {"horizon": "short", "threshold": "+5%", "direction": "bullish", bad: "x"}
            )


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
