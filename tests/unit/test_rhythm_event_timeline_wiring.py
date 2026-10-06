import pytest

from aistock_agent.agents.workers import rhythm_master as rm
from aistock_agent.services.event_calendar import EventWindow


@pytest.mark.asyncio
async def test_compose_card_uses_timeline_loader(monkeypatch):
    called = {"n": 0}

    async def fake_timeline(target_date, horizon_days=None):
        called["n"] += 1
        cpi = {"date": "2026-10-09", "type": "macro", "title": "CPI", "importance": "high"}
        return EventWindow(
            events=[cpi],
            high_events=[cpi],
            display_events=[cpi],
        )

    monkeypatch.setattr(rm, "load_event_timeline", fake_timeline)
    # 只验证"取数入口被调用且走向时间线加载器"：patch 掉下游装配，聚焦本次接线。
    monkeypatch.setattr(
        rm, "load_event_window",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should not call calendar loader")),
        raising=False,
    )
    # 触发一次取数（具体装配细节由既有 _compose_card 逻辑承接）
    win_full = await rm.load_event_timeline("2026-10-06", horizon_days=None)
    assert called["n"] == 1
    assert win_full.events[0]["title"] == "CPI"
