"""迭代 agent 阈值判断逻辑测试"""
import pytest


def test_threshold_normal_all_within_range():
    """所有指标在阈值内 → status=normal"""
    from aistock_agent.services.iterate_analyzer import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {
        "ma5": {"hit_rate": 0.6, "direction_accuracy": 0.5, "mean_deviation": 0.8,
                "attribution_match_rate": 0.4, "sentiment_bias": 0.08},
        "ma10": {"mean_deviation": 0.9},
        "ma20": {"sentiment_bias": 0.10},
    }
    triggered = check_thresholds(snapshot, rolling)
    assert triggered == []


def test_threshold_dim1_hit_rate_low():
    """维度一 hit_rate < 0.5 → 触发"""
    from aistock_agent.services.iterate_analyzer import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.3, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_1" in triggered


def test_threshold_dim1_new_coverage_high():
    """维度一 new_coverage_rate > 0.4 → 触发"""
    from aistock_agent.services.iterate_analyzer import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.5},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_1" in triggered


def test_threshold_dim2_abs_deviation_high():
    """维度二 abs(mean_deviation) > 3 → 触发"""
    from aistock_agent.services.iterate_analyzer import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": -4.0},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_2" in triggered


def test_threshold_dim2_ma10_mean_deviation_high():
    """维度二 MA10 均值偏差 > 1.5 → 触发"""
    from aistock_agent.services.iterate_analyzer import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 2.0}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_2" in triggered


def test_threshold_dim3_similarity_low():
    """维度三 attribution_match_rate < 0.3 → 触发（similarity < 3 近似）"""
    from aistock_agent.services.iterate_analyzer import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.2},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_3" in triggered


def test_threshold_dim4_ma20_bias_high():
    """维度四 MA20 sentiment_bias > 0.15 → 触发"""
    from aistock_agent.services.iterate_analyzer import check_thresholds

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.20}}
    triggered = check_thresholds(snapshot, rolling)
    assert "dimension_4" in triggered
