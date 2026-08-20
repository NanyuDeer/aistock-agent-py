"""日期工具 — A 股交易日判断

从 ``agents.workers.morning`` 迁入，供 morning agent 及未来其他模块复用。
"""

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from chinese_calendar import is_workday  # type: ignore[import-untyped]

from aistock_agent.config import settings


def is_trading_day(d: date | None = None) -> bool:
    """判断是否为 A 股交易日（排除周末和法定节假日）。

    Args:
        d: 指定日期，默认取今天。

    Notes:
        G6 缺陷背景：chinese_calendar 1.11.0 仅覆盖 2004-2026，2027 年起
        is_workday 抛 NotImplementedError，旧实现直接 fallback "只跳周末"，
        把 2027 法定节假日误判为交易日（精度损失）。
        本实现支持通过 ``HOLIDAYS_EXTRA``（config.holidays_extra，YYYY-MM-DD
        列表）注入覆盖范围之外的补充休市日，判定顺序：周末 → 补充表 →
        chinese_calendar → 越年 fallback。补充表语义为"休市日"，周末调休补班日
        不在支持范围（不承诺 2027 精确节假日）；chinese_calendar 库升级
        （覆盖 2027+）后 try 分支自动恢复精确判断，无需改代码。
    """
    target = d or date.today()
    if target.weekday() >= 5:
        return False
    # 补充节假日表优先（HOLIDAYS_EXTRA，YYYY-MM-DD；
    # chinese_calendar 覆盖 2004-2026 之外年份的精度来源）
    if settings.holidays_extra and target.isoformat() in settings.holidays_extra:
        return False
    try:
        return bool(is_workday(target))
    except (NotImplementedError, ValueError):
        # 越年（>2026）：有补充表则已排除休市日，剩余按交易日；
        # 无补充表回退"只跳周末"（语义与拆分前一致）
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


def shanghai_now() -> datetime:
    """返回上海时区的当前时刻（B-5 三时间戳采集用，防服务器/容器时区漂移）。"""
    return datetime.now(ZoneInfo("Asia/Shanghai"))


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
