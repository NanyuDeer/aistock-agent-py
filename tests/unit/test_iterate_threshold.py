"""迭代 agent 阈值判断逻辑测试"""


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


# ---------------------------------------------------------------------------
# build_scorecard — 四维确定性评分卡
# ---------------------------------------------------------------------------

_VALID_DIMENSIONS = ["dimension_1", "dimension_2", "dimension_3", "dimension_4"]


def _normal_snapshot():
    """返回全部正常的快照 + rolling_stats"""
    return (
        {
            "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
            "dimension_2_direction": {"mean_deviation": 0.5},
            "dimension_3_attribution": {"attribution_match_rate": 0.6},
            "dimension_4_sentiment": {"bias": 0.05},
        },
        {
            "ma5": {},
            "ma10": {"mean_deviation": 0.9},
            "ma20": {"sentiment_bias": 0.10},
        },
    )


def test_build_scorecard_contains_all_four_dimensions():
    """评分卡必须包含全部四个维度，每个维度有 metrics / thresholds / triggered"""
    from aistock_agent.services.iterate_analyzer import build_scorecard

    snapshot, rolling = _normal_snapshot()
    scorecard = build_scorecard(snapshot, rolling)

    for dim in _VALID_DIMENSIONS:
        assert dim in scorecard, f"scorecard 缺少 {dim}"
        entry = scorecard[dim]
        assert "metrics" in entry
        assert "thresholds" in entry
        assert "triggered" in entry
        assert isinstance(entry["triggered"], bool)


def test_build_scorecard_triggered_matches_check_thresholds():
    """评分卡的 triggered 标志必须与 check_thresholds() 结果一致"""
    from aistock_agent.services.iterate_analyzer import build_scorecard, check_thresholds

    # 构造只有 dimension_1 触发的数据
    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.3, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    rolling = {"ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}}

    triggered = check_thresholds(snapshot, rolling)
    scorecard = build_scorecard(snapshot, rolling)

    for dim in _VALID_DIMENSIONS:
        assert scorecard[dim]["triggered"] == (dim in triggered), (
            f"{dim} 的 scorecard.triggered 与 check_thresholds 不一致"
        )


def test_build_scorecard_normal_all_false():
    """全部正常时评分卡所有维度 triggered=False"""
    from aistock_agent.services.iterate_analyzer import build_scorecard

    snapshot, rolling = _normal_snapshot()
    scorecard = build_scorecard(snapshot, rolling)

    for dim in _VALID_DIMENSIONS:
        assert scorecard[dim]["triggered"] is False


# ---------------------------------------------------------------------------
# _sanitize_llm_output — LLM 输出清洗（确定性阈值为唯一真相）
# ---------------------------------------------------------------------------


def test_sanitize_overrides_triggered_dimensions():
    """LLM 返回的 triggered_dimensions 必须被确定性结果覆盖"""
    from aistock_agent.services.iterate_analyzer import _sanitize_llm_output

    llm_result = {
        "triggered_dimensions": ["dimension_1", "dimension_2", "dimension_4"],
        "analysis": {"dimension_1": {"summary": "偏差"}},
        "optimization_suggestions": [],
    }
    # 确定性结果只有 dimension_1 触发
    cleaned = _sanitize_llm_output(llm_result, ["dimension_1"], "2026-07-13")
    assert cleaned["triggered_dimensions"] == ["dimension_1"]


def test_sanitize_filters_untriggered_analysis_to_observations():
    """LLM 在 analysis 中返回了未触发维度 → 必须移到 observations，不能留在 analysis"""
    from aistock_agent.services.iterate_analyzer import _sanitize_llm_output

    llm_result = {
        "triggered_dimensions": ["dimension_1"],
        "analysis": {
            "dimension_1": {"summary": "已触发维度分析"},
            "dimension_2": {"summary": "未触发维度分析，不应出现在 analysis"},
            "dimension_4": {"summary": "另一个未触发维度"},
        },
        "optimization_suggestions": [],
    }
    cleaned = _sanitize_llm_output(llm_result, ["dimension_1"], "2026-07-13")

    # analysis 只保留已触发的 dimension_1
    assert "dimension_1" in cleaned["analysis"]
    assert "dimension_2" not in cleaned["analysis"]
    assert "dimension_4" not in cleaned["analysis"]

    # 未触发的被降级到 observations
    observations = cleaned.get("observations", [])
    obs_dims = [obs.get("dimension") for obs in observations]
    assert "dimension_2" in obs_dims
    assert "dimension_4" in obs_dims


def test_sanitize_filters_untriggered_suggestions_to_observations():
    """LLM 的 optimization_suggestions 引用未触发维度 → 降级到 observations"""
    from aistock_agent.services.iterate_analyzer import _sanitize_llm_output

    llm_result = {
        "triggered_dimensions": ["dimension_1"],
        "analysis": {"dimension_1": {"summary": "ok"}},
        "optimization_suggestions": [
            {
                "target": "morning_prompt",
                "suggestion": "基于 dimension_1 的建议",
                "priority": "high",
                "dimension": "dimension_1",
            },
            {
                "target": "morning_prompt",
                "suggestion": "基于 dimension_2 的建议",
                "priority": "high",
                "dimension": "dimension_2",
            },
            {
                "target": "morning_prompt",
                "suggestion": "基于 dimension_4 的建议",
                "priority": "medium",
                "dimension": "dimension_4",
            },
        ],
    }
    cleaned = _sanitize_llm_output(llm_result, ["dimension_1"], "2026-07-13")

    # 只有 dimension_1 的建议保留
    suggestions = cleaned["optimization_suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["dimension"] == "dimension_1"

    # dimension_2 / dimension_4 的建议降级到 observations
    observations = cleaned.get("observations", [])
    obs_dims = [obs.get("dimension") for obs in observations]
    assert "dimension_2" in obs_dims
    assert "dimension_4" in obs_dims


def test_sanitize_text_reference_untriggered_dimension():
    """suggestion 没有 dimension 字段但文本引用了未触发维度 → 降级"""
    from aistock_agent.services.iterate_analyzer import _sanitize_llm_output

    llm_result = {
        "triggered_dimensions": ["dimension_1"],
        "analysis": {"dimension_1": {"summary": "ok"}},
        "optimization_suggestions": [
            {
                "target": "morning_prompt",
                "suggestion": "针对 dimension_2 方向偏差的优化",
                "priority": "high",
            },
            {
                "target": "morning_prompt",
                "suggestion": "通用优化建议",
                "priority": "low",
            },
        ],
    }
    cleaned = _sanitize_llm_output(llm_result, ["dimension_1"], "2026-07-13")

    suggestions = cleaned["optimization_suggestions"]
    # 引用 dimension_2 的被降级，通用的保留
    assert len(suggestions) == 1
    assert "通用" in suggestions[0]["suggestion"]

    observations = cleaned.get("observations", [])
    assert len(observations) == 1
    assert "dimension_2" in str(observations[0].get("content", ""))


def test_sanitize_no_observations_when_all_triggered():
    """所有 analysis/suggestions 维度都已触发 → 不产生 observations"""
    from aistock_agent.services.iterate_analyzer import _sanitize_llm_output

    llm_result = {
        "triggered_dimensions": ["dimension_1", "dimension_2"],
        "analysis": {
            "dimension_1": {"summary": "ok"},
            "dimension_2": {"summary": "ok"},
        },
        "optimization_suggestions": [
            {"target": "morning_prompt", "suggestion": "建议1", "dimension": "dimension_1"},
            {"target": "morning_prompt", "suggestion": "建议2", "dimension": "dimension_2"},
        ],
    }
    cleaned = _sanitize_llm_output(llm_result, ["dimension_1", "dimension_2"], "2026-07-13")

    assert "observations" not in cleaned or len(cleaned.get("observations", [])) == 0
    assert len(cleaned["analysis"]) == 2
    assert len(cleaned["optimization_suggestions"]) == 2


# ---------------------------------------------------------------------------
# build_scorecard — evidence_kind 证据来源维度标注（B1）
# ---------------------------------------------------------------------------


def test_scorecard_has_evidence_kind():
    """每个维度的评分卡必须标注证据来源类型"""
    from aistock_agent.services.iterate_analyzer import build_scorecard

    snapshot = {
        "dimension_1_coverage": {"hit_rate": 0.9, "new_coverage_rate": 0.1},
        "dimension_2_direction": {"mean_deviation": 1.0},
        "dimension_3_attribution": {"attribution_match_rate": 0.8},
    }
    rolling = {"ma10": {"mean_deviation": 0.5}, "ma20": {"sentiment_bias": 0.05}}

    card = build_scorecard(snapshot, rolling)
    assert card["dimension_1"]["evidence_kind"] == "deterministic"
    assert card["dimension_3"]["evidence_kind"] == "llm_derived"
    assert card["dimension_4"]["evidence_kind"] == "llm_derived"
