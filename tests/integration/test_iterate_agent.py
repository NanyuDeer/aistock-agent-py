"""iterate_agent 集成测试"""
import json
from unittest.mock import MagicMock, patch

import pytest

from aistock_agent.agents.workers import iterate as iterate_agent


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.iterate._load_snapshot")
@patch("aistock_agent.agents.workers.iterate._load_rolling_stats")
async def test_iterate_normal(mock_rolling, mock_snapshot):
    """所有指标正常 → status=normal"""
    mock_snapshot.return_value = {
        "dimension_1_coverage": {"hit_rate": 0.7, "new_coverage_rate": 0.2},
        "dimension_2_direction": {"mean_deviation": 0.5},
        "dimension_3_attribution": {"attribution_match_rate": 0.6},
        "dimension_4_sentiment": {"bias": 0.05},
    }
    mock_rolling.return_value = {
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}
    }

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    with patch.object(iterate_agent, "_archive_iterate"):
        result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "normal"


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.iterate._load_snapshot")
@patch("aistock_agent.agents.workers.iterate._load_rolling_stats")
@patch("aistock_agent.agents.workers.iterate.get_deep_think")
@patch("aistock_agent.agents.workers.iterate._read_report_excerpt", return_value="")
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
        "ma5": {}, "ma10": {"mean_deviation": 0.9}, "ma20": {"sentiment_bias": 0.10}
    }
    mock_llm.return_value.invoke.return_value = MagicMock(content=json.dumps({
        "date": "2026-07-08",
        "status": "alert",
        "triggered_dimensions": ["dimension_1"],
        "analysis": {"dimension_1": {"summary": "hit_rate过低", "root_cause": "信息筛选问题"}},
        "optimization_suggestions": [{"target": "morning_prompt", "suggestion": "扩大信息源", "priority": "high"}]
    }))

    state = {
        "messages": [], "session_id": "test", "user_id": None,
        "favorites": [], "intent": None, "symbol": None, "tag_code": None,
        "analysis_reports": {}, "final_response": None,
    }
    with patch.object(iterate_agent, "_archive_iterate"):
        result = await iterate_agent.run(state)
    parsed = json.loads(result["final_response"])
    assert parsed["status"] == "alert"
    assert "dimension_1" in parsed["triggered_dimensions"]


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.iterate._load_snapshot", return_value=None)
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
