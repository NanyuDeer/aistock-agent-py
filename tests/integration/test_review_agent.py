"""review_agent 集成测试"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import review as review_agent


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value=None,
)
@patch("aistock_agent.agents.workers.review.set_cached_review", new_callable=AsyncMock)
@patch("aistock_agent.agents.workers.review.get_deep_think")
@patch("aistock_agent.agents.workers.review.get_tools")
async def test_review_run_success(mock_get_tools, _mock_get_llm, mock_set_cache, _mock_get_cache):
    """复盘 agent 正常执行：LLM 返回报告 → 缓存 + 返回"""
    from langchain_core.messages import AIMessage

    # mock agent
    mock_agent = AsyncMock()
    mock_agent.ainvoke.return_value = {
        "messages": [AIMessage(content="# 复盘报告\n上证综指涨0.5%...")]
    }
    mock_get_tools.return_value = []

    # patch create_react_agent + archive_review（已迁至 services/archiver.py）
    with patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock()) as save:
        with patch(
            "aistock_agent.agents.workers.review.create_react_agent",
            return_value=mock_agent,
        ):
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
                    "trigger_source": "scheduler",
                    "report_date": "2026-07-17",
                }
                result = await review_agent.run(state)

    assert "final_response" in result
    assert "复盘报告" in result["final_response"]
    mock_set_cache.assert_called_once()
    assert save.await_args.kwargs["content"]["schema_version"] == "2.0"
    details = save.await_args.kwargs["content"]["display_report"]["details"]
    assert details == result["final_response"]


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value="cached review",
)
async def test_review_run_cache_hit(_mock_cache):
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
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value=None,
)
@patch(
    "aistock_agent.agents.workers.review.get_deep_think",
    side_effect=Exception("LLM down"),
)
async def test_review_run_llm_failure(_mock_llm, _mock_cache):
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


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value="cached scheduler review",
)
async def test_cached_scheduler_review_is_still_persisted(_mock_cache):
    """scheduler 触发 + 缓存命中：仍需按 schema v2 持久化（供下游读取）"""
    with patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock()) as save:
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
            "trigger_source": "scheduler",
            "report_date": "2026-07-17",
        }
        result = await review_agent.run(state)

    assert result["final_response"] == "cached scheduler review"
    save.assert_awaited_once()
    kwargs = save.await_args.kwargs
    assert kwargs["report_type"] == "review"
    assert kwargs["report_date"] == "2026-07-17"
    assert kwargs["content"]["schema_version"] == "2.0"
    assert kwargs["content"]["display_report"]["details"] == "cached scheduler review"


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value="cached manual review",
)
async def test_cached_manual_review_is_not_persisted(_mock_cache):
    """手动触发（无 trigger_source='scheduler'）+ 缓存命中：不调用持久化"""
    with patch.object(review_agent.node_api, "save_analysis_report", new=AsyncMock()) as save:
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

    assert result["final_response"] == "cached manual review"
    save.assert_not_awaited()


@pytest.mark.asyncio
@patch(
    "aistock_agent.agents.workers.review.get_cached_review",
    new_callable=AsyncMock,
    return_value=None,
)
@patch("aistock_agent.agents.workers.review.set_cached_review", new_callable=AsyncMock)
@patch("aistock_agent.agents.workers.review.get_deep_think")
@patch("aistock_agent.agents.workers.review.get_tools")
async def test_scheduler_persist_failure_keeps_review_response(
    mock_get_tools, _mock_get_llm, _mock_set_cache, _mock_get_cache
):
    """scheduler 持久化失败不应影响复盘正常返回（降级吞掉异常）"""
    from langchain_core.messages import AIMessage

    mock_agent = AsyncMock()
    mock_agent.ainvoke.return_value = {
        "messages": [AIMessage(content="# 复盘报告\n上证综指涨0.5%...")]
    }
    mock_get_tools.return_value = []

    with patch.object(
        review_agent.node_api,
        "save_analysis_report",
        new=AsyncMock(side_effect=Exception("DB down")),
    ) as save:
        with patch(
            "aistock_agent.agents.workers.review.create_react_agent",
            return_value=mock_agent,
        ):
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
                    "trigger_source": "scheduler",
                    "report_date": "2026-07-17",
                }
                result = await review_agent.run(state)

    assert "复盘报告" in result["final_response"]
    save.assert_awaited_once()
