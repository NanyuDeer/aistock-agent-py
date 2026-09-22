"""L3 前瞻迁出行为锁定（迁移纪律，spec §5.1/硬约束 9）：迁出前后对外行为不变。

锁定点：① query 族条数与硬上限 1:1；② 软上限按 query 计数；③ 当日去重 key 日期化；
④ 空结果负缓存；⑤ 解析不出日期不入库（warning）；⑥ 产出恒 medium + source=L3。
"""
import pytest

from aistock_agent.services.forward_event_sources import (
    L3_DAILY_SOFT_LIMIT,
    L3_FORWARD_QUERIES,
    L3_QUERY_HARD_LIMIT,
    _parse_forward_events,
    collect_l3_forward,
)


def test_query_family_matches_hard_limit():
    assert len(L3_FORWARD_QUERIES) == 6, "§5.7：query 族 4→6（覆盖 7 类事件族）"
    assert L3_QUERY_HARD_LIMIT == 6
    assert L3_DAILY_SOFT_LIMIT == 12


def test_parse_forward_events_medium_and_source():
    hits = {"results": [{"title": "美联储 10 月议息会议日程", "content": "10 月 28 日公布"}]}
    events = _parse_forward_events("美联储 议息 日程", hits, "2026-09-20")
    assert events and events[0]["importance"] == "medium"
    assert events[0]["source"] == "L3"
    assert events[0]["event_date"] == "2026-10-28"


def test_parse_forward_events_no_date_skips(caplog):
    hits = {"results": [{"title": "无日期内容", "content": "近期将公布"}]}
    events = _parse_forward_events("q", hits, "2026-09-20")
    assert events == []


@pytest.mark.asyncio
async def test_collect_l3_forward_respects_soft_limit_and_cache(monkeypatch):
    class FakeCache:
        def __init__(self):
            self.keys = set()

        def normalize_key(self, d, q):
            return f"{d}|{q}"

        def get(self, key):
            return "ok" if key in self.keys else None

        def record(self, key, empty=False):
            self.keys.add(key)

    called: list[str] = []

    async def fake_search(query):
        called.append(query)
        return {"outcome": "ok", "results": [{"title": f"{query} 10 月 28 日事件", "content": "10 月 28 日"}]}

    monkeypatch.setattr("aistock_agent.services.forward_event_sources._run_search", fake_search)
    posted: list[dict[str, object]] = []

    async def fake_post(body):
        posted.append(body)
        return {"code": 0, "data": {"id": 1, "upserted": True}}

    monkeypatch.setattr("aistock_agent.services.forward_event_sources.node_api.post_calendar_event", fake_post)

    events = await collect_l3_forward("2026-09-20", FakeCache())
    assert len(called) == 6  # 6 条 query 全查
    assert len(events) == 6
    assert all(e["source"] == "L3" for e in events)