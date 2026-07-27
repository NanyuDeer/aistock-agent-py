"""日期工具 — A 股交易日判断

从 ``agents.workers.morning`` 迁入，供 morning agent 及未来其他模块复用。
"""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from chinese_calendar import is_workday  # type: ignore[import-untyped]


def is_trading_day(d: date | None = None) -> bool:
    """判断是否为 A 股交易日（排除周末和法定节假日）。

    Args:
        d: 指定日期，默认取今天。
    """
    target = d or date.today()
    if target.weekday() >= 5:
        return False
    return bool(is_workday(target))


def shanghai_today() -> date:
    """返回上海时区的自然日，作为报告交易日。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()
