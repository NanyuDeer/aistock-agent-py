"""升格路径删除回归（R3/§5.8.1）：L3 medium 不再被读侧升 high。"""
import pytest

import aistock_agent.services.event_calendar as m
from aistock_agent.services.event_calendar import load_event_window


@pytest.mark.asyncio
async def test_l3_medium_not_upgraded(monkeypatch):
    async def fake_get(d_from, d_to):
        return [
            {"date": "2026-10-01", "title": "美联储 10 月议息会议", "importance": "medium",
             "type": "macro", "source": "L3"},
        ]

    monkeypatch.setattr(
        "aistock_agent.services.event_calendar.node_api.get_calendar_events", fake_get
    )
    win = await load_event_window("2026-09-20", horizon_days=4)
    assert win.high_events == []
    assert win.events[0]["importance"] == "medium"  # 不再升 high


def test_high_only_from_seed():
    """high 只来自种子/晋升（source=L4）；抓取源封顶 medium（R3）。"""
    assert not hasattr(m, "is_high_importance_event")  # 读侧词表升格路径已删除
