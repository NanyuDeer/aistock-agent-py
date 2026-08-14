"""utils.date 测试 — A 股交易日判断（从 morning_agent 迁入）"""

from datetime import date
from unittest.mock import patch

from aistock_agent.utils.date import is_trading_day, prev_trading_day


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


def test_2027_weekday_falls_back_to_trading_day():
    """P2-2 越年 fallback：2027 超出 chinese_calendar 覆盖（2004-2026）→
    不抛 NotImplementedError，按可交易日处理（只跳周末）。"""
    # 2027-01-04 是周一
    assert is_trading_day(date(2027, 1, 4)) is True


def test_2027_weekend_still_not_trading_day():
    """越年 fallback 不影响周末判断：2027-01-02 周六仍非交易日。"""
    assert is_trading_day(date(2027, 1, 2)) is False


def test_2026_in_coverage_behavior_unchanged():
    """覆盖年份（2004-2026）行为不变：国庆节（2026-10-01）仍非交易日。"""
    assert is_trading_day(date(2026, 10, 1)) is False


def test_prev_trading_day_skips_weekend():
    """周六 → 前一个交易日为周五。"""
    assert prev_trading_day(date(2026, 8, 2)) == date(2026, 7, 31)


def test_prev_trading_day_skips_holiday():
    """国庆节（2026-10-01 周四）→ 前一个交易日为 2026-09-30（周三）。"""
    assert prev_trading_day(date(2026, 10, 1)) == date(2026, 9, 30)


def test_prev_trading_day_from_weekday():
    """周一 → 前一个交易日为上周五。"""
    assert prev_trading_day(date(2026, 8, 3)) == date(2026, 7, 31)
