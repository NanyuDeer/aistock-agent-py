"""iterate_agent 集成测试"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.agents.workers import iterate as iterate_agent

# 函数迁至 services/iterate_analyzer.py 后的 patch 路径
_ANALYZER = "aistock_agent.services.iterate_analyzer"


@pytest.mark.asyncio
async def test_iterate_uses_scheduler_report_date() -> None:
    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "report_date": "2026-07-23", "final_response": None,
    }
    with patch.object(
        iterate_agent,
        "analyze",
        new=AsyncMock(return_value={"date": "2026-07-23", "status": "normal"}),
    ) as analyze:
        await iterate_agent.run(state)

    analyze.assert_awaited_once_with("2026-07-23")


@pytest.mark.asyncio
@patch(f"{_ANALYZER}._load_snapshot")
@patch(f"{_ANALYZER}._load_rolling_stats")
async def test_iterate_normal(mock_rolling, mock_snapshot):
    """所有指标正常 → status=normal"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10},
    }

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    with patch(f"{_ANALYZER}._archive_iterate"):
        result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "normal"


@pytest.mark.asyncio
@patch(f"{_ANALYZER}._load_snapshot")
@patch(f"{_ANALYZER}._load_rolling_stats")
@patch(f"{_ANALYZER}.get_deep_think")
@patch(f"{_ANALYZER}._read_report_excerpt", return_value="")
async def test_iterate_alert(mock_excerpt, mock_llm, mock_rolling, mock_snapshot):
    """阈值触发 → LLM 生成分析报告"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.3, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
        "morning_file": "test.md",
        "review_file": "test.md",
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10},
    }
    mock_llm.return_value.invoke.return_value = MagicMock(content=json.dumps({
        "date": "2026-07-08",
        "status": "alert",
        "triggered_dimensions": ["dimension_1"],
        "analysis": {"dimension_1": {"summary": "hit_rate过低", "root_cause": "信息筛选问题"}},
        "optimization_suggestions": [
            {"target": "morning_prompt", "suggestion": "扩大信息源", "priority": "high"},
        ],
    }))

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    with patch(f"{_ANALYZER}._archive_iterate"):
        result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "alert"
    assert "dimension_1" in parsed["triggered_dimensions"]


@pytest.mark.asyncio
@patch(f"{_ANALYZER}._load_snapshot", return_value=None)
async def test_iterate_no_snapshot(mock_snapshot):
    """快照不存在 → status=skip"""
    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "skip"


@pytest.mark.asyncio
@patch(f"{_ANALYZER}._load_snapshot")
@patch(f"{_ANALYZER}._load_rolling_stats")
async def test_iterate_normal_includes_scorecard(mock_rolling, mock_snapshot):
    """normal 状态也必须包含四维确定性评分卡"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10},
    }

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    with patch(f"{_ANALYZER}._archive_iterate"):
        result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])

    assert parsed["status"] == "normal"
    assert "scorecard" in parsed
    for dim in ("dimension_1", "dimension_2", "dimension_3", "dimension_4"):
        assert dim in parsed["scorecard"]
        assert parsed["scorecard"][dim]["triggered"] is False
    assert parsed["triggered_dimensions"] == []


@pytest.mark.asyncio
@patch(f"{_ANALYZER}._load_snapshot")
@patch(f"{_ANALYZER}._load_rolling_stats")
@patch(f"{_ANALYZER}.get_deep_think")
@patch(f"{_ANALYZER}._read_report_excerpt", return_value="")
async def test_iterate_alert_has_scorecard(mock_excerpt, mock_llm, mock_rolling, mock_snapshot):
    """alert 状态也必须包含四维确定性评分卡"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.3, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
        "morning_file": "test.md",
        "review_file": "test.md",
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10},
    }
    mock_llm.return_value.invoke.return_value = MagicMock(content=json.dumps({
        "date": "2026-07-08",
        "status": "alert",
        "triggered_dimensions": ["dimension_1"],
        "analysis": {"dimension_1": {"summary": "hit_rate过低", "root_cause": "信息筛选问题"}},
        "optimization_suggestions": [
            {"target": "morning_prompt", "suggestion": "扩大信息源", "priority": "high",
             "dimension": "dimension_1"},
        ],
    }))

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    with patch(f"{_ANALYZER}._archive_iterate"):
        result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])

    assert parsed["status"] == "alert"
    assert "scorecard" in parsed
    for dim in ("dimension_1", "dimension_2", "dimension_3", "dimension_4"):
        assert dim in parsed["scorecard"]
    # 只有 dimension_1 触发
    assert parsed["scorecard"]["dimension_1"]["triggered"] is True
    assert parsed["scorecard"]["dimension_2"]["triggered"] is False


@pytest.mark.asyncio
@patch(f"{_ANALYZER}._load_snapshot")
@patch(f"{_ANALYZER}._load_rolling_stats")
@patch(f"{_ANALYZER}.get_deep_think")
@patch(f"{_ANALYZER}._read_report_excerpt", return_value="")
async def test_iterate_alert_filters_untriggered_llm_content(
    mock_excerpt, mock_llm, mock_rolling, mock_snapshot,
):
    """核心场景：只有 dimension_1 触发，但 LLM 返回了 dimension_2/4 的分析和建议
    → 最终归档结果必须过滤/降级这些内容"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.3, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
        "morning_file": "test.md",
        "review_file": "test.md",
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10},
    }
    # LLM 返回了未触发的 dimension_2 和 dimension_4 内容
    mock_llm.return_value.invoke.return_value = MagicMock(content=json.dumps({
        "date": "2026-07-13",
        "status": "alert",
        "triggered_dimensions": ["dimension_1", "dimension_2", "dimension_4"],
        "analysis": {
            "dimension_1": {"summary": "hit_rate过低", "root_cause": "信息筛选问题"},
            "dimension_2": {"summary": "方向偏差分析", "root_cause": "不该出现的分析"},
            "dimension_4": {"summary": "情绪偏差分析", "root_cause": "不该出现的分析"},
        },
        "optimization_suggestions": [
            {"target": "morning_prompt", "suggestion": "基于 d1 的建议", "priority": "high",
             "dimension": "dimension_1"},
            {"target": "morning_prompt", "suggestion": "基于 d2 的建议", "priority": "high",
             "dimension": "dimension_2"},
            {"target": "morning_prompt", "suggestion": "基于 d4 的建议", "priority": "medium",
             "dimension": "dimension_4"},
        ],
    }))

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    with patch(f"{_ANALYZER}._archive_iterate"):
        result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])

    # 1. triggered_dimensions 必须是确定性结果，不能信任 LLM
    assert parsed["triggered_dimensions"] == ["dimension_1"]

    # 2. analysis 只允许包含已触发维度
    assert "dimension_1" in parsed["analysis"]
    assert "dimension_2" not in parsed["analysis"]
    assert "dimension_4" not in parsed["analysis"]

    # 3. optimization_suggestions 只允许基于已触发维度
    suggestions = parsed["optimization_suggestions"]
    assert len(suggestions) == 1
    assert suggestions[0]["dimension"] == "dimension_1"

    # 4. 未触发维度内容降级到 observations
    observations = parsed.get("observations", [])
    obs_dims = [obs.get("dimension") for obs in observations]
    assert "dimension_2" in obs_dims
    assert "dimension_4" in obs_dims

    # 5. scorecard 仍然包含全部四个维度
    assert "scorecard" in parsed
    assert parsed["scorecard"]["dimension_1"]["triggered"] is True
    assert parsed["scorecard"]["dimension_2"]["triggered"] is False
    assert parsed["scorecard"]["dimension_4"]["triggered"] is False
