from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_scrape_sources import (
    collect_cls_telegraph,
    collect_eastmoney_judgements,
    collect_global_markets,
    collect_tavily,
    collect_ths_original,
)
from aistock_agent.tools.market_tools import GlobalMarketFetchError


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


@pytest.mark.asyncio
async def test_collect_eastmoney_judgements_fails_returns_empty():
    """异常降级：node_api.get 抛异常 → 返回 []（采集层永不 500）。"""
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node down"))
        events = await collect_eastmoney_judgements("2026-08-12")
        assert events == []


@pytest.mark.asyncio
async def test_collect_ths_original_normalizes_items():
    """正常路径：items → 归一化 EventRecord（source=ths_original）。"""
    rows = [
        {
            "source_id": "s1",
            "title": "同花顺原创标题",
            "content": "正文内容",
            "keywords": ["半导体", "靶材"],
        }
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": rows})
        events = await collect_ths_original("2026-08-12")
        assert len(events) == 1
        assert events[0]["source"] == "ths_original"
        assert events[0]["summary"] == "正文内容"
        assert events[0]["involved_keywords"] == ["半导体", "靶材"]


@pytest.mark.asyncio
async def test_collect_ths_original_handles_json_string_keywords():
    """Min-1：keywords 为 JSONB，Node 可能返回 JSON 字符串，防御解析；失败 → []。"""
    rows = [
        {"title": "A", "content": "x", "keywords": '["半导体", "靶材"]'},
        {"title": "B", "content": "y", "keywords": "not-json"},
    ]
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"items": rows})
        events = await collect_ths_original("2026-08-12")
        assert len(events) == 2
        assert events[0]["involved_keywords"] == ["半导体", "靶材"]
        assert events[1]["involved_keywords"] == []


@pytest.mark.asyncio
async def test_collect_ths_original_fails_returns_empty():
    """异常降级：node_api.get 抛异常 → 返回 []。"""
    with patch("aistock_agent.services.event_scrape_sources.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node down"))
        events = await collect_ths_original("2026-08-12")
        assert events == []


class _FakeTavily:
    @staticmethod
    def search(query: str, **kwargs: object) -> dict[str, object]:
        return {
            "results": [
                {"title": "政策利好", "content": "摘要", "url": "https://tavily.com/1"}
            ]
        }


class _FakeTavilyFail:
    @staticmethod
    def search(query: str, **kwargs: object) -> dict[str, object]:
        raise RuntimeError("tavily down")


@pytest.mark.asyncio
async def test_collect_tavily_normalizes_results():
    """正常路径：TavilyService.search 返回 results → 归一化（source=tavily）。"""
    # collect_tavily 函数内 from-import TavilyService，patch 源模块 tavily.TavilyService
    with patch("aistock_agent.services.tavily.TavilyService", new=_FakeTavily):
        events = await collect_tavily("2026-08-12")
    assert len(events) == 2  # 两个 query 各返回一条（含去重前同 content_hash）
    assert all(ev["source"] == "tavily" for ev in events)
    assert events[0]["title"] == "政策利好"
    assert events[0]["url"] == "https://tavily.com/1"


@pytest.mark.asyncio
async def test_collect_tavily_search_fails_returns_empty():
    """异常降级：search 抛异常 → 返回 []（逐 query 捕获 continue）。"""
    with patch("aistock_agent.services.tavily.TavilyService", new=_FakeTavilyFail):
        events = await collect_tavily("2026-08-12")
    assert events == []


@pytest.mark.asyncio
async def test_collect_global_markets_normalizes_facts():
    """正常路径：结构化事实 → 归一化（source=global_markets，>=1% 波动 impact_score=5）。"""
    facts = [
        {"ticker": "NDX", "name": "纳斯达克", "price": 21000.0, "change_pct": 1.5},
        {"ticker": "HSI", "name": "恒生指数", "price": 22000.0, "change_pct": 0.5},
        # Min-5：name/ticker 均为空 → 跳过
        {"ticker": "", "name": "", "price": 1.0, "change_pct": 2.0},
    ]
    # collect_global_markets 函数内 from-import collect_global_market_facts，
    # patch 源模块 market_tools.collect_global_market_facts
    with patch(
        "aistock_agent.tools.market_tools.collect_global_market_facts",
        new=AsyncMock(return_value=facts),
    ):
        events = await collect_global_markets()
    assert len(events) == 2
    assert all(ev["source"] == "global_markets" for ev in events)
    assert events[0]["impact_score"] == 5  # |1.5| >= 1 记为重大事实
    assert events[1]["impact_score"] == 1  # |0.5| < 1 普通事实


@pytest.mark.asyncio
async def test_collect_global_markets_fails_returns_empty():
    """异常降级：collect_global_market_facts 抛 GlobalMarketFetchError → 返回 []。"""
    with patch(
        "aistock_agent.tools.market_tools.collect_global_market_facts",
        new=AsyncMock(side_effect=GlobalMarketFetchError("node down")),
    ):
        events = await collect_global_markets()
    assert events == []
