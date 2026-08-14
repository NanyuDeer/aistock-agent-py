from datetime import date

import pytest

from aistock_agent.utils.date import add_trading_days


def test_skip_weekend():
    # 2026-08-07 是周五；+1 交易日应跨周末到 2026-08-10（周一）
    assert add_trading_days(date(2026, 8, 7), 1) == date(2026, 8, 10)


def test_count_multiple():
    # 2026-08-10（周一）+ 5 交易日 = 2026-08-17（周一）
    assert add_trading_days(date(2026, 8, 10), 5) == date(2026, 8, 17)


def test_zero_returns_same():
    assert add_trading_days(date(2026, 8, 10), 0) == date(2026, 8, 10)


def test_negative_raises():
    with pytest.raises(ValueError):
        add_trading_days(date(2026, 8, 10), -1)


def test_cross_year_120_days_lands_in_2027():
    """Bug B 根因回归：long 档 +120 交易日跨入 2027 不再抛 NotImplementedError，
    返回 2027 年内日期（越年 fallback 只跳周末）。"""
    result = add_trading_days(date(2026, 8, 13), 120)
    assert result.year == 2027
    assert result == date(2027, 2, 5)
