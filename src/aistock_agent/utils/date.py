"""日期工具 — A 股交易日判断

从 ``agents.workers.morning`` 迁入，供 morning agent 及未来其他模块复用。
"""

from datetime import date, datetime, time, timedelta
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


def prev_trading_day(d: date | None = None) -> date:
    """返回指定日期（默认今天）之前最近的一个交易日（不含当天）。

    用于非交易日提示：向前回溯跳过周末与法定节假日，返回最近交易日。
    """
    target = d or date.today()
    cursor = target - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def shanghai_today() -> date:
    """返回上海时区的自然日，作为报告交易日。"""
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def is_trading_time(now: datetime | None = None) -> bool:
    """判断当前是否在 A 股交易时段（9:30-11:30 / 13:00-15:00 北京时间）。"""
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not is_trading_day(now.date()):
        return False
    t = now.time()
    return time(9, 30) <= t <= time(11, 30) or time(13, 0) <= t <= time(15, 0)


def trading_session_status(now: datetime | None = None) -> tuple[str, str]:
    """返回交易时段状态 + 提示文案。

    Returns:
        (status, hint_text)
        status: "trading" | "pre_open" | "lunch_break" | "closed" | "non_trading_day"
    """
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if not is_trading_day(now.date()):
        last = prev_trading_day(now.date())
        return "non_trading_day", f"今天非交易日，最近交易日 {last.isoformat()}"
    t = now.time()
    if t < time(9, 30):
        return "pre_open", "今日开盘前（开盘时间 09:30）"
    if t <= time(11, 30):
        return "trading", ""
    if t < time(13, 0):
        return "lunch_break", "午间休市，13:00 复盘"
    if t <= time(15, 0):
        return "trading", ""
    return "closed", "今日已收盘"
