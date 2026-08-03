"""utils/date.py 非交易时段判断单测。"""
from datetime import datetime
from zoneinfo import ZoneInfo

from aistock_agent.utils.date import is_trading_time, trading_session_status


SH = ZoneInfo("Asia/Shanghai")


def _dt(h: int, m: int, weekday: int = 0) -> datetime:
    """构造北京时间 datetime（weekday 0=Mon）。"""
    # 2026-08-03 是周一，便于构造非节假日工作日场景
    return datetime(2026, 8, 3 + weekday, h, m, tzinfo=SH)


def test_is_trading_time_morning_session():
    assert is_trading_time(_dt(9, 30)) is True
    assert is_trading_time(_dt(10, 0)) is True
    assert is_trading_time(_dt(11, 30)) is True


def test_is_trading_time_afternoon_session():
    assert is_trading_time(_dt(13, 0)) is True
    assert is_trading_time(_dt(14, 0)) is True
    assert is_trading_time(_dt(15, 0)) is True


def test_is_trading_time_pre_open():
    assert is_trading_time(_dt(9, 29)) is False
    assert is_trading_time(_dt(8, 53)) is False  # 用户复现时间点


def test_is_trading_time_lunch_break():
    assert is_trading_time(_dt(11, 31)) is False
    assert is_trading_time(_dt(12, 0)) is False
    assert is_trading_time(_dt(12, 59)) is False


def test_is_trading_time_closed():
    assert is_trading_time(_dt(15, 1)) is False
    assert is_trading_time(_dt(23, 0)) is False


def test_is_trading_time_weekend():
    # 2026-08-08 是周六
    assert is_trading_time(datetime(2026, 8, 8, 10, 0, tzinfo=SH)) is False


def test_trading_session_status_returns_trading_during_session():
    status, hint = trading_session_status(_dt(10, 0))
    assert status == "trading"
    assert hint == ""


def test_trading_session_status_pre_open():
    status, hint = trading_session_status(_dt(8, 53))
    assert status == "pre_open"
    assert "09:30" in hint


def test_trading_session_status_lunch_break():
    status, hint = trading_session_status(_dt(12, 0))
    assert status == "lunch_break"
    assert "13:00" in hint


def test_trading_session_status_closed():
    status, _hint = trading_session_status(_dt(15, 1))
    assert status == "closed"


def test_trading_session_status_non_trading_day():
    status, hint = trading_session_status(datetime(2026, 8, 8, 10, 0, tzinfo=SH))
    assert status == "non_trading_day"
    assert "非交易日" in hint
