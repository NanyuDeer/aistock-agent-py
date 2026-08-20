from datetime import date

import pytest

from aistock_agent.utils import date as date_module
from aistock_agent.utils.date import add_trading_days, is_trading_day


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


# =============================================================================
# is_trading_day 补充节假日表（HOLIDAYS_EXTRA）—— G6 越年精度
# =============================================================================


def test_is_trading_day_2027_in_holidays_extra_is_non_trading(monkeypatch):
    """越年（2027-01-01，周五）在补充表 → 非交易日。

    chinese_calendar 1.11.0 仅覆盖 2004-2026；2027-01-01 落在 fallback 区。
    补充表含该日时必须按休市处理（否则 fallback 会把法定节假日当交易日）。
    """
    monkeypatch.setattr(date_module.settings, "holidays_extra", ["2027-01-01"])
    assert is_trading_day(date(2027, 1, 1)) is False


def test_is_trading_day_2027_weekday_not_in_holidays_extra_is_trading(monkeypatch):
    """越年（2027-01-04，周一）不在补充表 → 交易日（fallback 只跳周末）。"""
    monkeypatch.setattr(date_module.settings, "holidays_extra", ["2027-01-01"])
    assert is_trading_day(date(2027, 1, 4)) is True


def test_add_trading_days_cross_2027_skips_holidays_extra(monkeypatch):
    """跨 2027 且补充表含 2027-01-01 → 正确跳过（对比无补充表结果差异）。"""
    # 无补充表：fallback 将 2027-01-01（周五）视为交易日 → +1 落在 2027-01-01
    assert add_trading_days(date(2026, 12, 31), 1) == date(2027, 1, 1)
    # 有补充表：2027-01-01 休市 → 跨周末落到 2027-01-04（周一）
    monkeypatch.setattr(date_module.settings, "holidays_extra", ["2027-01-01"])
    assert add_trading_days(date(2026, 12, 31), 1) == date(2027, 1, 4)
