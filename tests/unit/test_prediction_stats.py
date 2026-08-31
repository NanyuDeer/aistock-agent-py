# tests/unit/test_prediction_stats.py
from aistock_agent.services.prediction_stats import (
    baseline_compare,
    baseline_neutral_summary,
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


def test_clamp_respects_cap_floor():
    hit = {"n": 40, "hits": 6, "hit_rate": 0.15, "ci": (0.06, 0.30)}
    base = {"n": 40, "hits": 24, "hit_rate": 0.60}
    cap, _ = clamp_confidence_by_bucket("short", hit, base, cap_floor="low")
    assert cap == "low"
