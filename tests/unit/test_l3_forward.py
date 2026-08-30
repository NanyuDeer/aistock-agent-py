"""L3 前瞻捕捉（§4.3/§4.8）：4 query 硬上限 + 8 次软上限 + 日期解析 + 负缓存。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import event_scrape_sources as src
from aistock_agent.services.search_cache import SearchCache


@pytest.fixture
def cache() -> SearchCache:
    return SearchCache()


@pytest.fixture
def mock_tavily(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    def fake_search(query, **kwargs):
        if "财经日历" in query:
            return {"results": [{"title": "下周财经日历：9月2日发布8月PMI", "content": "9月2日 9:30 发布 8月官方制造业PMI", "url": "https://x.example/1"}], "provider": "anysearch", "outcome": "ok"}
        return {"results": [], "provider": "anysearch", "outcome": "empty"}
    m = AsyncMock(side_effect=lambda q, **kw: fake_search(q, **kw))
    monkeypatch.setattr(src, "_run_search", m)
    return m


@pytest.mark.asyncio
async def test_collect_l3_forward_parses_date_and_upserts(mock_tavily: AsyncMock, cache: SearchCache, monkeypatch: pytest.MonkeyPatch) -> None:
    posted: list[dict] = []
    monkeypatch.setattr(src.node_api, "post_calendar_event", AsyncMock(side_effect=lambda b: posted.append(b) or {"id": 1, "upserted": True}))
    events = await src.collect_l3_forward("2026-08-28", cache)
    assert len(events) >= 1
    assert events[0]["source"] == "L3"
    assert posted and posted[0]["source"] == "L3"


@pytest.mark.asyncio
async def test_hard_limit_four_queries(cache: SearchCache, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(src, "_run_search", AsyncMock(side_effect=lambda q: calls.append(q) or {"results": [], "provider": "anysearch", "outcome": "empty"}))
    monkeypatch.setattr(src.node_api, "post_calendar_event", AsyncMock(return_value={"id": 1, "upserted": True}))
    await src.collect_l3_forward("2026-08-28", cache)
    assert len(src.L3_FORWARD_QUERIES) == 4
    assert len(calls) == 4  # 硬上限 4 条/日


@pytest.mark.asyncio
async def test_soft_limit_eight_per_day(cache: SearchCache, monkeypatch: pytest.MonkeyPatch) -> None:
    """软上限 8 次/日（provider failover 重试不计入）：同日重复调用累计超 8 跳过后续。"""
    monkeypatch.setattr(src, "_run_search", AsyncMock(return_value={"results": [], "provider": "anysearch", "outcome": "empty"}))
    monkeypatch.setattr(src.node_api, "post_calendar_event", AsyncMock(return_value={"id": 1, "upserted": True}))
    src._l3_daily_count = {"2026-08-28": 8}
    # 同日已用满 8 → 直接跳过
    events = await src.collect_l3_forward("2026-08-28", cache)
    assert events == []
    src._l3_daily_count = {}


@pytest.mark.asyncio
async def test_cache_skip_second_call_same_day(cache: SearchCache, monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(src, "_run_search", AsyncMock(side_effect=lambda q: called.append(q) or {"results": [], "provider": "anysearch", "outcome": "empty"}))
    monkeypatch.setattr(src.node_api, "post_calendar_event", AsyncMock(return_value={"id": 1, "upserted": True}))
    await src.collect_l3_forward("2026-08-28", cache)
    n1 = len(called)
    await src.collect_l3_forward("2026-08-28", cache)
    assert len(called) == n1  # 当日去重（成功记录）
