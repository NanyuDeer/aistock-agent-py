from datetime import date

import pytest

from aistock_agent.utils.date import trading_days_between


def test_same_day_zero():
    assert trading_days_between(date(2026, 9, 14), date(2026, 9, 14)) == 0


def test_reversed_zero():
    assert trading_days_between(date(2026, 9, 16), date(2026, 9, 14)) == 0


def test_skips_weekend_fri_to_tue():
    # 2026-09-18(周五) -> 2026-09-22(周二)：交易日 {09-21, 09-22}
    assert trading_days_between(date(2026, 9, 18), date(2026, 9, 22)) == 2


def test_weekday_direct():
    assert trading_days_between(date(2026, 9, 14), date(2026, 9, 16)) == 2


def test_out_of_calendar_year_returns_none():
    assert trading_days_between(date(2027, 1, 4), date(2027, 1, 8)) is None
