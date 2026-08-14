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

    Notes:
        越年 fallback：chinese_calendar 1.11.0 仅覆盖 2004-2026，2027 年数据需
        2026 年底公布。目标日期超出覆盖范围时 is_workday 抛 NotImplementedError，
        此处捕获并按可交易日处理（保守可交易，只跳周末）；库更新后自动恢复精确判断。
    """
    target = d or date.today()
    if target.weekday() >= 5:
        return False
    try:
        return bool(is_workday(target))
    except (NotImplementedError, ValueError):
        # 越年 fallback：该年度节假日数据尚未发布/库未覆盖。无法确认法定节假日时
        # 按可交易日处理（保守可交易），待库更新后 is_workday 不再抛异常，自动恢复。
        return True


def prev_trading_day(d: date | None = None) -> date:
    """返回指定日期（默认今天）之前最近的一个交易日（不含当天）。

    用于非交易日提示：向前回溯跳过周末与法定节假日，返回最近交易日。
    """
    target = d or date.today()
    cursor = target - timedelta(days=1)
    while not is_trading_day(cursor):
        cursor -= timedelta(days=1)
    return cursor


def add_trading_days(d: date, n: int) -> date:
    """从 d 起向后推进 n 个交易日（不含 d 本身）。

    用于预测到期日确定性计算：horizon 分档 → 交易日偏移。
    n 必须 >= 0；跨周末与法定节假日自动跳过。
    """
    if n < 0:
        raise ValueError("n must be >= 0")
    cursor = d
    advanced = 0
    while advanced < n:
        cursor += timedelta(days=1)
        if is_trading_day(cursor):
            advanced += 1
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
        return "lunch_break", "午间休市（13:00 复盘）"
    if t <= time(15, 0):
        return "trading", ""
    return "closed", "今日已收盘"
