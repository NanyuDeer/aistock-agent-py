"""review_agent 集成测试"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import review as review_agent


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.review.get_cached_review", new_callable=AsyncMock, return_value=None)
@patch("aistock_agent.agents.workers.review.set_cached_review", new_callable=AsyncMock)
@patch("aistock_agent.agents.workers.review.get_deep_think")
@patch("aistock_agent.agents.workers.review.get_tools")
async def test_review_run_success(mock_get_tools, mock_get_llm, mock_set_cache, mock_get_cache):
    """复盘 agent 正常执行：LLM 返回报告 → 缓存 + 返回"""
    from langchain_core.messages import AIMessage

    # mock LLM + agent
    mock_llm = mock_get_llm.return_value
    mock_agent = AsyncMock()
    mock_agent.ainvoke.return_value = {
        "messages": [AIMessage(content="# 复盘报告\n上证综指涨0.5%...")]
    }
    mock_get_tools.return_value = []

    # patch create_react_agent + archive_review（已迁至 services/archiver.py）
    with patch("aistock_agent.agents.workers.review.create_react_agent", return_value=mock_agent):
        with patch("aistock_agent.agents.workers.review.archive_review"):
            state = {
                "messages": [],
                "session_id": "test",
                "user_id": None,
                "favorites": [],
                "intent": None,
                "symbol": None,
                "tag_code": None,
                "analysis_reports": {},
                "final_response": None,
            }
            result = await review_agent.run(state)

    assert "final_response" in result
    assert "复盘报告" in result["final_response"]
    mock_set_cache.assert_called_once()


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.review.get_cached_review", new_callable=AsyncMock, return_value="cached review")
async def test_review_run_cache_hit(mock_cache):
    """缓存命中：直接返回缓存内容，不调用 LLM"""
    state = {
        "messages": [],
        "session_id": "test",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }
    result = await review_agent.run(state)
    assert result["final_response"] == "cached review"


@pytest.mark.asyncio
@patch("aistock_agent.agents.workers.review.get_cached_review", new_callable=AsyncMock, return_value=None)
@patch("aistock_agent.agents.workers.review.get_deep_think", side_effect=Exception("LLM down"))
async def test_review_run_llm_failure(mock_llm, mock_cache):
    """LLM 异常：返回降级文本"""
    state = {
        "messages": [],
        "session_id": "test",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }
    result = await review_agent.run(state)
    assert "暂时不可用" in result["final_response"]
