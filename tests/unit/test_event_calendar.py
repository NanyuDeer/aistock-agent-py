"""事件窗口聚合（§4.1/§4.2/§4.6）：消费 /internal/calendar/events + 存量披露密度。"""
from unittest.mock import AsyncMock

import pytest

from aistock_agent.services import event_calendar
from aistock_agent.services.event_calendar import load_event_window


@pytest.fixture
def mock_api(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    api = AsyncMock()
    monkeypatch.setattr(event_calendar, "node_api", api)
    return api


@pytest.mark.asyncio
async def test_load_event_window_returns_events(mock_api: AsyncMock) -> None:
    mock_api.get_calendar_events.return_value = [
        {"date": "2026-09-01", "type": "delivery", "title": "交割日", "importance": "medium", "source": "L1"},
        {"date": "2026-09-02", "type": "earnings", "title": "英伟达财报", "importance": "high", "source": "L3"},
    ]
    win = await load_event_window("2026-09-01")
    assert len(win.events) == 2
    assert len(win.high_events) == 1
    assert win.high_events[0]["importance"] == "high"
    assert win.source_missing is False
    # 窗口上界 = target_date 起 5 个交易日（dateTo 传参验证）
    _date_from, date_to = mock_api.get_calendar_events.call_args.args
    assert date_to == "2026-09-08"


@pytest.mark.asyncio
async def test_load_event_window_empty_not_missing(mock_api: AsyncMock) -> None:
    """空态区分：空数组 = 正常无事件；None = 数据源未接（§4.6）。"""
    mock_api.get_calendar_events.return_value = []
    win = await load_event_window("2026-09-01")
    assert win.events == [] and win.source_missing is False


@pytest.mark.asyncio
async def test_load_event_window_none_is_source_missing(mock_api: AsyncMock) -> None:
    mock_api.get_calendar_events.return_value = None
    win = await load_event_window("2026-09-01")
    assert win.events == [] and win.source_missing is True


@pytest.mark.asyncio
async def test_earnings_density(mock_api: AsyncMock) -> None:
    mock_api.get.return_value = {"density": [{"date": "2026-09-02", "count": 42}]}
    density = await event_calendar.load_earnings_density("2026-09-01", "2026-09-05")
    assert density == [{"date": "2026-09-02", "count": 42}]


@pytest.mark.asyncio
async def test_load_event_window_calendar_uncovered(mock_api: AsyncMock, monkeypatch: pytest.MonkeyPatch) -> None:
    """交易日历越年 → fail-close 标记（§16 开放问题 6），不抛错、不各自兜底。"""
    import datetime as _dt

    def _raise(d: _dt.date, n: int) -> _dt.date:
        raise ValueError("calendar out of range")

    monkeypatch.setattr(event_calendar, "add_trading_days", _raise)
    win = await load_event_window("2027-01-04")
    assert win.calendar_uncovered is True
    assert win.events == []
