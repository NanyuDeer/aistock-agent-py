# tests/unit/test_prediction_stats.py
from aistock_agent.services.prediction_stats import (
    baseline_compare,
    baseline_neutral_summary,
    build_validation_profile,
    bucket_summary,
    clamp_confidence_by_bucket,
    hit_rate_summary,
    wilson_ci,
)


def test_wilson_ci_basic():
    lo, hi = wilson_ci(10, 10)
    assert lo > 0.6 and hi == 1.0


def test_wilson_ci_zero_n():
    assert wilson_ci(0, 0) == (0.0, 0.0)


def test_hit_rate_summary_filters_v2_only():
    entries = [
        {"result": "hit", "methodology_version": "2.0"},
        {"result": "miss", "methodology_version": "2.0"},
        {"result": "hit", "methodology_version": "1.0"},   # v1 不参与（H1 分桶）
        {"result": "insufficient", "methodology_version": "2.0"},  # 不参与分母（P0-2）
        {"result": "hit", "methodology_version": "2.0", "approximate": True},  # D2：近似档剔除
    ]
    s = hit_rate_summary(entries)
    assert s["n"] == 2
    assert s["hits"] == 1
    assert s["hit_rate"] == 0.5
    assert s["sufficient_sample"] is False


def test_hit_rate_summary_sample_threshold():
    entries = [{"result": "hit", "methodology_version": "2.0"} for _ in range(30)]
    assert hit_rate_summary(entries)["sufficient_sample"] is True


def test_baseline_compare_excess():
    llm = {"n": 30, "hit_rate": 0.6}
    base = {"n": 30, "hit_rate": 0.4}
    r = baseline_compare(llm, base)
    assert r["excess"] == 0.2
    assert r["better_than_baseline"] is True


def _entry(target_type, result="hit", prediction_id=1, methodology_version="2.0"):
    return {"methodology_version": methodology_version, "result": result, "target_type": target_type,
            "approximate": False, "prediction_id": prediction_id}


def test_hit_rate_summary_default_filters_v2_only():
    """阶段 0：默认（不传版本）只统计 2.0——混合 1.0/2.0/3.0 记录时 n 只含 2.0（防跳变/混桶）。"""
    entries = [
        _entry("index", "hit", 1, "1.0"),
        _entry("index", "hit", 2, "2.0"),
        _entry("index", "miss", 2, "2.0"),
        _entry("index", "hit", 3, "3.0"),
    ]
    s = hit_rate_summary(entries)
    assert s["n"] == 2 and s["hits"] == 1


def test_hit_rate_summary_filters_by_methodology_version():
    """阶段 0：显式传 methodology_version='3.0' 只统计 3.0 记录（2.0 被隔离，观测通道）。"""
    entries = [
        _entry("index", "hit", 1, "2.0"),
        _entry("index", "miss", 1, "2.0"),
        _entry("index", "hit", 2, "3.0"),
        _entry("index", "hit", 3, "3.0"),
    ]
    s = hit_rate_summary(entries, methodology_version="3.0")
    assert s["n"] == 2 and s["hits"] == 2
    assert s["hit_rate"] == 1.0


def test_bucket_summary_filters_by_methodology_version():
    """阶段 0：bucket_summary 传版本只统计该版本（3.0 桶观测）。"""
    entries = [
        _entry("index", "hit", 1, "2.0"),
        _entry("index", "hit", 2, "3.0"),
        _entry("sector", "miss", 2, "3.0"),
    ]
    b = bucket_summary(entries, methodology_version="3.0")
    assert b["combined"]["n"] == 2 and b["combined"]["hits"] == 1
    assert b["index"]["n"] == 1 and b["sector"]["n"] == 1
    assert b["index"]["hits"] == 1 and b["sector"]["hits"] == 0


def test_baseline_neutral_summary_filters_by_methodology_version():
    """阶段 0：baseline 同套版本过滤（3.0 记录单独分桶）。"""
    entries = [
        _entry("index", "hit", 1, "2.0"),
        _entry("index", "hit", 2, "3.0"),
    ]
    # 仅 3.0 记录无 baseline_neutral 字段 → n=0（被 _filter_v2 后的 bool 过滤剔除）
    b = baseline_neutral_summary(entries, methodology_version="3.0")
    assert b["n"] == 0


def test_hit_rate_summary_filters_by_target_type():
    entries = [_entry("index", "hit", 1), _entry("index", "miss", 1),
               _entry("sector", "hit", 2), _entry("sector", "miss", 2)]
    s = hit_rate_summary(entries, target_type="sector")
    assert s["n"] == 2 and s["hits"] == 1


def test_bucket_summary_separates_index_sector():
    entries = [_entry("index", "hit", 1), _entry("index", "miss", 1),
               _entry("sector", "hit", 2), _entry("sector", "miss", 2)]
    b = bucket_summary(entries)
    assert b["index"]["n"] == 2 and b["sector"]["n"] == 2
    assert b["combined"]["n"] == 4


def test_bucket_summary_n_predictions_dedup():
    # 同 prediction 三档（1 个 prediction_id）→ n_predictions=1
    entries = [_entry("sector", "hit", 7), _entry("sector", "hit", 7), _entry("sector", "miss", 7)]
    b = bucket_summary(entries)
    assert b["sector"]["n_predictions"] == 1
    assert b["sector"]["n"] == 3


def test_sufficient_sample_requires_both_counts():
    # 30 档但仅 1 个 prediction → 不 sufficient（样本独立性，H4）
    entries = [_entry("sector", "hit", 1)] * 30
    b = bucket_summary(entries)
    assert b["sector"]["sufficient_sample"] is False


def test_clamp_triggers_when_ci_upper_below_baseline():
    hit = {"n": 40, "hits": 10, "hit_rate": 0.25, "ci": (0.13, 0.41)}
    base = {"n": 40, "hits": 24, "hit_rate": 0.60}
    cap, reason = clamp_confidence_by_bucket("short", hit, base)
    assert cap == "medium"
    assert "钳制" in reason


def test_clamp_no_action_when_sample_insufficient():
    hit = {"n": 10, "hits": 3, "hit_rate": 0.3, "ci": (0.10, 0.60)}
    base = {"n": 10, "hits": 6, "hit_rate": 0.6}
    cap, reason = clamp_confidence_by_bucket("short", hit, base)
    assert cap is None
    assert "样本不足" in reason


def test_clamp_high_when_not_worse_than_baseline():
    hit = {"n": 40, "hits": 28, "hit_rate": 0.7, "ci": (0.54, 0.82)}
    base = {"n": 40, "hits": 20, "hit_rate": 0.5}
    cap, reason = clamp_confidence_by_bucket("short", hit, base)
    assert cap == "high"


def _v3_entry(result="hit", horizon="short", grade=None, method="3.0",
              condition_index=None, condition_met=None, **kw):
    e = {"methodology_version": method, "result": result, "horizon": horizon,
         "target_type": "stock", "approximate": False, **kw}
    if grade is not None:
        e["grade"] = grade
    if condition_index is not None:
        e["condition_index"] = condition_index
        e["condition_met"] = condition_met
    return e


# 画像针对 run_once 当前写入的 3.0 现役档（_METHODOLOGY_VERSION）；stats 默认 2.0 是
# 存量统计口径，画像读取/接管需显式传 3.0 才能框住现役已验证档（防混桶）。
_PROFILE_V3 = {"methodology_version": "3.0"}


def test_build_validation_profile_empty():
    """Spec B §7 P1：空 entries → 画像零值且不抛异常。"""
    p = build_validation_profile([], "600519", **_PROFILE_V3)
    assert p["target"] == "600519"
    assert p["n"] == 0 and p["hit_rate"] == 0.0
    assert p["sufficient_sample"] is False
    assert p["condition_met_rate"] is None
    assert p["miss_patterns"] == []
    assert p["horizon_breakdown"] == {}
    assert p["degradation_rate"] == 0.0


def test_build_validation_profile_hit_rate():
    """Spec B §7 P1：单 target 命中率/n/样本判定正确；非当前版本 & insufficient & approximate 剔除。"""
    entries = [
        _v3_entry("hit"),
        _v3_entry("miss"),
        _v3_entry("hit", method="2.0"),                     # 非当前版本剔除
        _v3_entry("insufficient", subtype="no_data"),       # 不计命中率
        _v3_entry("miss", approximate=True),                # 近似档剔除
    ]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    assert p["n"] == 2 and p["hits"] == 1 and p["hit_rate"] == 0.5
    assert p["sufficient_sample"] is False
    # insufficient 单列降解占比（2 可判 + 1 insufficient）
    assert p["degradation_rate"] == round(1 / 3, 4)


def test_build_validation_profile_horizon_breakdown():
    """Spec B §7 P1：horizon_breakdown 按档位分桶命中率。"""
    entries = [_v3_entry("hit", horizon="short"),
               _v3_entry("miss", horizon="mid"),
               _v3_entry("hit", horizon="short")]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    assert p["horizon_breakdown"]["short"]["n"] == 2
    assert p["horizon_breakdown"]["short"]["hit_rate"] == 1.0
    assert p["horizon_breakdown"]["mid"]["n"] == 1
    assert p["horizon_breakdown"]["mid"]["hit_rate"] == 0.0


def test_build_validation_profile_miss_patterns():
    """Spec B §7 P1：miss_patterns 归类——strong_miss→strong_reversal，其余 plain_miss，按 count 降序。"""
    entries = [
        _v3_entry("miss", grade="strong_miss"),
        _v3_entry("miss"),
        _v3_entry("miss"),
    ]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    by = {x["pattern"]: x["count"] for x in p["miss_patterns"]}
    assert by == {"plain_miss": 2, "strong_reversal": 1}


def test_build_validation_profile_condition_met_distribution():
    """Spec B §7 P1：condition_met 分布（c{i} 汇总 + 整体命中率），None 计 confirmed=0。"""
    entries = [
        _v3_entry(condition_index=0, condition_met=True),
        _v3_entry(condition_index=0, condition_met=False),
        _v3_entry(condition_index=0, condition_met=None),  # 两段判定推迟
        _v3_entry(condition_index=1, condition_met=True),
    ]
    p = build_validation_profile(entries, "600519", **_PROFILE_V3)
    # condition_met_rate 只在已确认（非 None）样本上算：2 个 True / 3 个 confirmed
    assert p["condition_met_rate"] == round(2 / 3, 4)
    c0 = p["condition_summary"]["c0"]
    assert c0 == {"count": 3, "met": 1, "confirmed": 2}


def test_build_validation_profile_sample_threshold():
    """Spec B §7 P1：样本充足（30 档 + 30 prediction）→ sufficient_sample。"""
    entries = [_v3_entry("hit", prediction_id=i) for i in range(30)]
    assert build_validation_profile(entries, "600519", **_PROFILE_V3)["sufficient_sample"] is True


def test_clamp_respects_cap_floor():
    hit = {"n": 40, "hits": 6, "hit_rate": 0.15, "ci": (0.06, 0.30)}
    base = {"n": 40, "hits": 24, "hit_rate": 0.60}
    cap, _ = clamp_confidence_by_bucket("short", hit, base, cap_floor="low")
    assert cap == "low"
