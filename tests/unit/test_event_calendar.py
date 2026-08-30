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
        {
            "date": "2026-09-01",
            "type": "delivery",
            "title": "交割日",
            "importance": "medium",
            "source": "L1",
        },
        {
            "date": "2026-09-02",
            "type": "earnings",
            "title": "英伟达财报",
            "importance": "high",
            "source": "L3",
        },
    ]
    win = await load_event_window("2026-09-01")
    assert len(win.events) == 2
    assert len(win.high_events) == 1
    assert win.high_events[0]["importance"] == "high"
    assert win.source_missing is False
    # 窗口 = 含 target_date 当日共 5 个交易日（§4.6）：2026-09-01 起第 4 个后续交易日
    date_from, date_to = mock_api.get_calendar_events.call_args.args
    assert date_from == "2026-09-01"
    assert date_to == "2026-09-07"


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
async def test_earnings_density_non_dict_returns_empty(mock_api: AsyncMock) -> None:
    """非 dict 响应（接口异常/错配）→ []，不抛错（§4.2）。"""
    mock_api.get.return_value = ["unexpected"]
    density = await event_calendar.load_earnings_density("2026-09-01", "2026-09-05")
    assert density == []


@pytest.mark.asyncio
async def test_load_event_window_real_year_crossing_fail_close(mock_api: AsyncMock) -> None:
    """真实越年（2027-01-04 超出 chinese_calendar 2004-2026 覆盖）→ fail-close（§16 开放问题 6）。

    显式判覆盖年份：不调 add_trading_days、不查接口、不各自兜底。
    """
    win = await load_event_window("2027-01-04")
    assert win.calendar_uncovered is True
    assert win.events == []
    mock_api.get_calendar_events.assert_not_called()


@pytest.mark.asyncio
async def test_load_event_window_calendar_uncovered_defensive(
    mock_api: AsyncMock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """防御路径：年份在覆盖内但 add_trading_days 抛 ValueError（如 horizon<0）→ 同一 fail-close。

    真实越年路径由 test_load_event_window_real_year_crossing_fail_close 覆盖。
    """
    import datetime as _dt

    def _raise(d: _dt.date, n: int) -> _dt.date:
        raise ValueError("calendar out of range")

    monkeypatch.setattr(event_calendar, "add_trading_days", _raise)
    win = await load_event_window("2026-09-01")
    assert win.calendar_uncovered is True
    assert win.events == []
