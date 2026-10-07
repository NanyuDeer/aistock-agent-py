import pytest

from aistock_agent.services import event_timeline
from aistock_agent.services.event_timeline import _merge_events, load_event_timeline


def test_merge_events_schedules_first_and_dedup_and_sorted():
    schedules = [
        {"date": "2026-10-09", "title": "交割日", "type": "delivery", "importance": "medium"}
    ]
    events = [
        {"date": "2026-10-16", "title": "CPI", "type": "macro", "importance": "high"},
        # dup
        {"date": "2026-10-09", "title": "交割日", "type": "delivery", "importance": "medium"},
    ]
    out = _merge_events(schedules, events)
    assert [e["title"] for e in out] == ["交割日", "CPI"]


@pytest.mark.asyncio
async def test_load_event_timeline_source_missing(monkeypatch):
    async def fake_get_entities(params):
        return None

    monkeypatch.setattr(event_timeline.node_api, "get_event_entities", fake_get_entities)
    win = await load_event_timeline("2026-10-06")
    assert win.source_missing is True
    assert win.events == []


@pytest.mark.asyncio
async def test_load_event_timeline_merges_l1_and_timeline(monkeypatch):
    async def fake_get_entities(params):
        return [
            {"title": "9月CPI发布", "event_start_time": "2026-10-09", "source_type": "calendar"},
        ]

    async def fake_get_calendar(date_from, date_to):
        return [
            {"date": "2026-10-06", "title": "10月股指期货交割日", "type": "delivery",
             "importance": "medium", "source": "L1"},
            {"date": "2026-10-07", "title": "某DB行", "type": "macro",
             "importance": "medium", "source": "L3"},  # 非 L1 → 丢弃
        ]

    monkeypatch.setattr(event_timeline.node_api, "get_event_entities", fake_get_entities)
    monkeypatch.setattr(event_timeline.node_api, "get_calendar_events", fake_get_calendar)
    win = await load_event_timeline("2026-10-06")
    titles = [e["title"] for e in win.events]
    assert titles == ["10月股指期货交割日", "9月CPI发布"]
    assert win.source_missing is False
    assert [e["title"] for e in win.high_events] == ["9月CPI发布"]


@pytest.mark.asyncio
async def test_load_event_timeline_calendar_uncovered():
    win = await load_event_timeline("2035-01-01")
    assert win.calendar_uncovered is True


@pytest.mark.asyncio
async def test_load_event_timeline_horizon_none_displays_full_window(monkeypatch):
    captured: dict[str, object] = {}

    async def fake_get_entities(params):
        captured["params"] = params
        return [
            {"title": "9月CPI发布", "event_start_time": "2026-10-09", "source_type": "calendar"},
            {"title": "年末重要会议", "event_start_time": "2026-12-20", "source_type": "manual"},
        ]

    async def fake_get_calendar(date_from, date_to):
        return []

    monkeypatch.setattr(event_timeline.node_api, "get_event_entities", fake_get_entities)
    monkeypatch.setattr(event_timeline.node_api, "get_calendar_events", fake_get_calendar)
    win = await load_event_timeline("2026-10-06", horizon_days=None)
    assert win.events
    assert win.display_events == win.events
    assert captured["params"]["dateTo"] == "2026-12-31"
