"""morning_agent 测试"""
import pytest
from datetime import date

from aistock_agent.agents.morning_agent import is_trading_day


def test_is_trading_day_weekday():
    # 2026-07-06 是周一
    assert is_trading_day(date(2026, 7, 6)) is True


def test_is_trading_day_saturday():
    # 2026-07-04 是周六
    assert is_trading_day(date(2026, 7, 4)) is False


def test_is_trading_day_national_holiday():
    # 2026-10-01 是国庆节
    assert is_trading_day(date(2026, 10, 1)) is False


def test_is_trading_day_no_arg_returns_bool():
    # 不传参数时调用 date.today()，验证不崩溃且返回 bool
    result = is_trading_day()
    assert isinstance(result, bool)
