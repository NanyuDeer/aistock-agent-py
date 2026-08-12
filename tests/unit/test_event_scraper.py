from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_scraper import run_event_scrape


@pytest.mark.asyncio
async def test_run_event_scrape_full_daily():
    with patch(
        "aistock_agent.services.event_scrape_sources.collect_cls_telegraph",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_eastmoney_judgements",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_ths_original",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_tavily",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_scrape_sources.collect_global_markets",
        new=AsyncMock(return_value=[]),
    ), patch(
        "aistock_agent.services.event_store.save_event_scrape",
        new=AsyncMock(return_value={"persisted": 0, "deduped": 0, "error": None}),
    ):
        result = await run_event_scrape("full_daily", score_date="2026-08-12")
    assert result["scrape_mode"] == "full_daily"
    assert result["persisted"] == 0


@pytest.mark.asyncio
async def test_run_event_scrape_unknown_mode_raises():
    with pytest.raises(ValueError, match="unknown scrape_mode"):
        await run_event_scrape("unknown_mode", score_date="2026-08-12")
