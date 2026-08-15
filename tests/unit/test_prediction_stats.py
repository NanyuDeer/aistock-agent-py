# tests/unit/test_prediction_stats.py
from aistock_agent.services.prediction_stats import baseline_compare, hit_rate_summary, wilson_ci


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
