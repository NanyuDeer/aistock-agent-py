"""evaluator —— 预判验证评分（evaluate_verification）确定性单测（Spec C P1）。

评分口径（Spec C §4.2，与归因评分器并列不混用）：
- 基础分：到期 hit_rate（确定性，来自验证 entry）
- 方向维度：direction 与到期实际方向一致性（复用归因 direction 判定思路）
- condition 维度：condition_met_rate（条件成立命中率）+ miss_patterns 归类

纯确定性评分，不调 LLM，按 present 维度权重重归一化（对齐 evaluate_attribution）。
"""

from aistock_agent.iterate.evaluator import evaluate_verification, VerificationScore
from aistock_agent.schemas.prediction import (
    PredictionAnchor,
    PredictionCondition,
    PredictionHorizon,
    PredictionResult,
    PredictionRisk,
)


def _horizon(horizon: str, direction: str, *,
             remaining_estimate: str = "2 周", phase: str = "building",
             target: str = "上证指数", confidence: str = "high") -> PredictionHorizon:
    return PredictionHorizon(
        horizon=horizon, remaining_estimate=remaining_estimate, phase=phase,
        direction=direction, target=target, metric_projection="收盘涨跌幅",
        confidence=confidence,
    )


def _prediction(*, directions: dict[str, str] | None = None,
                conditions: list[PredictionCondition] | None = None) -> PredictionResult:
    dirs = directions or {"short": "bullish"}
    return PredictionResult(
        schema_version="3.0",
        prediction_status="confirmed",
        horizons=[_horizon(h, d) for h, d in dirs.items()],
        conditions=conditions or [],
        evolution_narrative="走强",
        risks=[PredictionRisk(factor="f", invalidation="v")],
        evolution_steps=[],
        evidence_ids=[],
    )


def _cond(i: int, *, horizon: str = "short", direction: str = "bullish") -> PredictionCondition:
    return PredictionCondition(
        condition=f"条件 {i}",
        scenario=f"情景 {i}",
        anchor=PredictionAnchor(horizon=horizon, threshold="+5%", direction=direction),
    )


def _entry(*, horizon: str = "short", result: str = "hit", actual: str = "+2.00%",
           grade: str | None = None, condition_index: int | None = None,
           condition_met: bool | None = None) -> dict:
    e: dict = {
        "horizon": horizon,
        "result": result,
        "actual": actual,
        "methodology_version": "3.0",
        "approximate": False,
        "prediction_id": 1,
        "target_type": "index",
    }
    if grade is not None:
        e["grade"] = grade
    if condition_index is not None:
        e["condition_index"] = condition_index
    if condition_met is not None:
        e["condition_met"] = condition_met
    return e


def _hit_entry(actual: str = "+2.00%") -> dict:
    return _entry(result="hit", actual=actual)


def _miss_entry(actual: str = "-2.00%") -> dict:
    return _entry(result="miss", actual=actual)


def _score(base: PredictionResult, entries: list[dict]) -> VerificationScore:
    return evaluate_verification(base, entries)


# ---------------------------------------------------------------------------
# 空画像降级
# ---------------------------------------------------------------------------

def test_empty_verification_degrades() -> None:
    """空验证 entry → 降级：score=0，hit_rate=0，gap 说明"无已验证样本"。"""
    base = _prediction()
    s = evaluate_verification(base, [])
    assert s.score == 0.0
    assert s.hit_rate == 0.0
    assert "无已验证样本" in s.gap_analysis


def test_only_non_judged_entries_degrades() -> None:
    """只有 insufficient 等不可判 entry → 无 hit/miss → 同样降级为 0。"""
    base = _prediction()
    entries = [
        {"horizon": "short", "result": "insufficient", "actual": "",
         "methodology_version": "3.0", "approximate": False, "prediction_id": 1,
         "target_type": "index"},
    ]
    s = evaluate_verification(base, entries)
    assert s.score == 0.0
    assert s.n == 0


# ---------------------------------------------------------------------------
# 基础分 hit_rate
# ---------------------------------------------------------------------------

def test_higher_hit_rate_scores_higher() -> None:
    """方向全一致时，hit_rate 越高综合分越高。"""
    base = _prediction()
    low = _score(base, [_hit_entry(), _miss_entry(), _miss_entry(), _miss_entry()])  # 0.25
    high = _score(base, [_hit_entry(), _hit_entry(), _hit_entry(), _miss_entry()])   # 0.75
    assert high.hit_rate > low.hit_rate
    assert high.score > low.score


def test_hit_rate_excludes_approximate() -> None:
    """approximate 档剔除，不进 hit_rate 分母。"""
    base = _prediction()
    entries = [
        _hit_entry(),
        _hit_entry(),
        {"horizon": "short", "result": "miss", "actual": "-2.00%",
         "methodology_version": "3.0", "approximate": True, "prediction_id": 1,
         "target_type": "index"},
    ]
    s = evaluate_verification(base, entries)
    assert s.hit_rate == 1.0  # 2 hit-miss 判档，全 hit；approximate 剔除
    assert s.n == 2


# ---------------------------------------------------------------------------
# 方向维度
# ---------------------------------------------------------------------------

def test_direction_consistency_losses_score_when_wrong() -> None:
    """方向不一致（预测 bullish、实际负向）时方向维失真，综合分下降。"""
    base_mixed = _prediction(directions={"short": "bullish", "mid": "bullish"})
    # 两档都预测 bullish：short 实际 +2（一致，hit），mid 实际 -3（反向，miss）
    consistent = _score(base_mixed, [
        _entry(horizon="short", result="hit", actual="+2.00%"),
        _entry(horizon="mid", result="hit", actual="+1.00%"),
    ])
    base_wrong = _prediction(directions={"short": "bullish", "mid": "bullish"})
    wrong = _score(base_wrong, [
        _entry(horizon="short", result="hit", actual="+2.00%"),
        _entry(horizon="mid", result="miss", actual="-3.00%"),
    ])
    # 方向一致性：wrong 有一档方向相反 → direction_score 更低
    assert wrong.direction_score < consistent.direction_score
    assert wrong.score < consistent.score


def test_neutral_prediction_direction_uses_threshold() -> None:
    """neutral 预测：实际横盘（|pct|<0.5%）视为方向一致。"""
    base = _prediction(directions={"short": "neutral"})
    s = _score(base, [_entry(horizon="short", result="hit", actual="+0.30%")])
    assert s.direction_score == 1.0


# ---------------------------------------------------------------------------
# condition 维度
# ---------------------------------------------------------------------------

def test_condition_met_rate_affects_score() -> None:
    """condition_met 成立率参与评分；越高综合分越高。"""
    conds = [_cond(0), _cond(1), _cond(2)]
    base = _prediction(conditions=conds)
    high = _score(base, [
        _entry(condition_index=0, condition_met=True, result="hit"),
        _entry(condition_index=1, condition_met=True, result="hit"),
        _entry(condition_index=2, condition_met=True, result="hit"),
    ])
    low = _score(base, [
        _entry(condition_index=0, condition_met=True, result="hit"),
        _entry(condition_index=1, condition_met=False, result="hit"),
        _entry(condition_index=2, condition_met=False, result="hit"),
    ])
    assert high.condition_met_rate == 1.0
    assert high.condition_met_rate > low.condition_met_rate
    assert high.score > low.score


def test_no_condition_dimension_renormalizes() -> None:
    """无 only-condition entry（无 condition_met）→ condition 维度不参与分母。"""
    base = _prediction()
    s = _score(base, [_hit_entry(), _hit_entry()])
    # 只有 hit_rate(0.5) + direction(0.3) 参与；condition(0.2) 剔除
    assert s.available_weight == 0.8
    assert s.score == 1.0


# ---------------------------------------------------------------------------
# miss_patterns → gap_analysis
# ---------------------------------------------------------------------------

def test_miss_patterns_strong_reversal_in_gap() -> None:
    """strong_miss 归类为强反向失效，反映到 gap_analysis。"""
    base = _prediction()
    s = _score(base, [
        _entry(result="miss", actual="-2.00%"),
        _entry(result="miss", actual="-6.00%", grade="strong_miss"),
        _entry(result="hit", actual="+2.00%"),
    ])
    assert s.miss_insights  # 非空
    patterns = {p["pattern"]: p["count"] for p in s.miss_insights}
    assert patterns.get("strong_reversal", 0) == 1
    assert "强反向" in s.gap_analysis