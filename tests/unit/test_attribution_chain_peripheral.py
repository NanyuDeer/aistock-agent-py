import pytest


@pytest.mark.asyncio
async def test_warehouse_pool_excludes_peripheral_and_root_lists_them(monkeypatch):
    import aistock_agent.services.attribution_chain as ac

    rows = [
        {"event_id": "a", "title": "半导体板块大涨", "summary": "", "source": "cls"},
        {"event_id": "b", "title": "恒生指数 隔夜表现", "summary": "", "source": "global_markets"},
    ]

    async def fake_load(_date):
        return rows

    # 函数内 from-import 在调用时读取源模块属性 → patch 源模块（from-import 绑定陷阱）
    monkeypatch.setattr("aistock_agent.services.event_store.load_event_scrape", fake_load)

    assert [e["event_id"] for e in await ac.load_chain_warehouse_events("2026-10-15")] == ["a"]
    assert [e["event_id"] for e in await ac.load_chain_peripheral_events("2026-10-15")] == ["b"]
    # include_peripheral=True 时全量返回（供诊断）
    assert [
        e["event_id"]
        for e in await ac.load_chain_warehouse_events("2026-10-15", include_peripheral=True)
    ] == ["a", "b"]


def test_peripheral_event_refs_maps_caps_and_sorts():
    from aistock_agent.services.attribution_chain import _peripheral_event_refs

    events = [
        {
            "event_id": f"e{i}",
            "title": f"标题{i}",
            "url": f"https://x/{i}",
            "impact_score": i,
            "scrape_at": f"2026-10-15T0{i}:00:00",
        }
        for i in range(1, 7)
    ]
    refs = _peripheral_event_refs(events)
    assert len(refs) == 5  # 上限 5
    assert [r["event_id"] for r in refs] == ["e6", "e5", "e4", "e3", "e2"]  # impact 降序
    assert refs[0] == {
        "event_id": "e6",
        "ref": "https://x/6",
        "headline": "标题6",
        "occurred_at": "2026-10-15T06:00:00",
    }
    assert _peripheral_event_refs([{"title": "无 id", "impact_score": 5}]) == []  # 无 event_id → 丢
