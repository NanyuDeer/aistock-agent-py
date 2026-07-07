"""日期工具 — A 股交易日判断

从 ``agents.workers.morning`` 迁入，供 morning agent 及未来其他模块复用。
"""

from datetime import date

from chinese_calendar import is_workday  # type: ignore[import-untyped]


def is_trading_day(d: date | None = None) -> bool:
    """判断是否为 A 股交易日（排除周末和法定节假日）。

    Args:
        d: 指定日期，默认取今天。
    """
    return bool(is_workday(d or date.today()))
