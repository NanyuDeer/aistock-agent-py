"""趋势评分报告持久化测试。"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from langchain_core.messages import AIMessage


@pytest.mark.asyncio
async def test_scheduler_persists_trend_score_with_data_source(monkeypatch):
    """调度触发时，趋势评分报告必须记录生成数据源。"""
    from aistock_agent.agents.workers import trend_score

    agent = MagicMock()
    agent.ainvoke = AsyncMock(
        return_value={
            "messages": [AIMessage(content='{"display_report": {"summary": "趋势结论"}}')]
        }
    )
    save_report = AsyncMock()
    monkeypatch.setattr(trend_score, "get_deep_think", MagicMock())
    monkeypatch.setattr(trend_score, "get_tools", lambda _: [])
    monkeypatch.setattr(trend_score, "create_react_agent", lambda *_: agent)
    monkeypatch.setattr(trend_score, "_archive_trend_score", lambda _: None)
    monkeypatch.setattr(
        trend_score.node_api,
        "get_list",
        AsyncMock(return_value=[{"symbol": "600519"}]),
    )
    monkeypatch.setattr(trend_score.node_api, "save_analysis_report", save_report)

    await trend_score.run({
        "messages": [],
        "analysis_reports": {},
        "trigger_source": "scheduler",
        "report_date": "2026-07-10",
    })

    assert save_report.await_args.kwargs["data_source"] == "trend_score_agent"
