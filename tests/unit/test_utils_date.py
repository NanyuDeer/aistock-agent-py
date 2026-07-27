"""utils.date 测试 — A 股交易日判断（从 morning_agent 迁入）"""

from datetime import date
from unittest.mock import patch

from aistock_agent.utils.date import is_trading_day


def test_weekday_is_trading_day():
    # 2026-07-06 是周一
    assert is_trading_day(date(2026, 7, 6)) is True


def test_saturday_not_trading_day():
    # 2026-07-04 是周六
    assert is_trading_day(date(2026, 7, 4)) is False


def test_make_up_workday_saturday_is_still_not_trading_day():
    """法定调休周六仍不是 A 股交易日。"""
    with patch("aistock_agent.utils.date.is_workday", return_value=True) as workday:
        assert is_trading_day(date(2026, 10, 10)) is False

    workday.assert_not_called()


def test_national_holiday_not_trading_day():
    # 2026-10-01 国庆节
    assert is_trading_day(date(2026, 10, 1)) is False


def test_no_arg_returns_bool():
    # 不传参数时调用 date.today()，验证不崩溃且返回 bool
    assert isinstance(is_trading_day(), bool)
