"""utils.date 测试 — A 股交易日判断（从 morning_agent 迁入）"""

from datetime import date

from aistock_agent.utils.date import is_trading_day


def test_weekday_is_trading_day():
    # 2026-07-06 是周一
    assert is_trading_day(date(2026, 7, 6)) is True


def test_saturday_not_trading_day():
    # 2026-07-04 是周六
    assert is_trading_day(date(2026, 7, 4)) is False


def test_national_holiday_not_trading_day():
    # 2026-10-01 国庆节
    assert is_trading_day(date(2026, 10, 1)) is False


def test_no_arg_returns_bool():
    # 不传参数时调用 date.today()，验证不崩溃且返回 bool
    assert isinstance(is_trading_day(), bool)
