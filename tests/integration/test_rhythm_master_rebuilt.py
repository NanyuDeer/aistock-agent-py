# tests/integration/test_rhythm_master_rebuilt.py
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.rhythm_master import run


@pytest.mark.asyncio
async def test_after_close_produces_master_card():
    with patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_index_kline",
        AsyncMock(return_value=[{"close": 120, "high": 121, "low": 119, "amount": 5000} for _ in range(65)]),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_fear_greed",
        AsyncMock(return_value={"index": 65}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.load_event_window",
        AsyncMock(return_value=type("W", (), {"events": [], "high_events": [], "source_missing": False, "calendar_uncovered": False})()),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.run_synthesis",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master._load_sentiment_series",
        return_value=([], [35, 40, 45, 50, 55], 0, None),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.get_last_close_snapshot",
        AsyncMock(return_value={"breadth": {"advance_count": 3800, "decline_count": 900, "total_count": 5000}}),
    ), patch(
        "aistock_agent.agents.workers.rhythm_master.node_api.save_analysis_report",
        AsyncMock(return_value={"id": 1}),
    ):
        result = await run({"refresh_slot": "after_close", "report_date": "2026-09-03"})
        content = result["analysis_reports"]["rhythm_master"]
        assert content["schema_version"] == "1.0"
        assert "evidence" in content
        assert "synthesis_available" in content
