from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_scrape_sources import (
    collect_cls_telegraph,
    collect_eastmoney_judgements,
)


@pytest.mark.asyncio
async def test_collect_cls_telegraph_normalizes_items():
    raw_items = [
        {
            "id": "1001",
            "title": "央行降准",
            "content": "央行宣布降准0.5个百分点",
            "time": "2026-08-12 09:30:00",
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": raw_items})
        events = await collect_cls_telegraph("2026-08-12")
        assert len(events) == 1
        assert events[0]["title"] == "央行降准"
        assert events[0]["source"] == "cls"
        assert events[0]["url"] == "https://www.cls.cn/detail/1001"


@pytest.mark.asyncio
async def test_collect_cls_telegraph_falls_back_to_latest():
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        async def fake_get(path, **kwargs):
            if "telegraph" in path:
                raise RuntimeError("telegraph failed")
            return {"items": [{"id": "2001", "title": "降级事件", "content": "x"}]}

        mock_api.get = AsyncMock(side_effect=fake_get)
        events = await collect_cls_telegraph("2026-08-12")
        assert len(events) == 1
        assert events[0]["title"] == "降级事件"


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_reads_existing_table():
    rows = [
        {
            "title": "某公司重大资产重组",
            "ai_summary": "重组预案披露",
            "ai_impact": "重大利好",
            "symbol": "600000",
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": rows})
        events = await collect_eastmoney_judgements("2026-08-12")
        assert len(events) == 1
        assert events[0]["source"] == "eastmoney"
