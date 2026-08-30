"""事件日历聚合（spec §4.1/§4.2/§4.6）。

- 消费 app-api `/internal/calendar/events`（L1 交割日 + market_calendar_events 合并，
  Python 侧不重复实现交割日计算）；
- 存量披露密度走 `/internal/calendar/earnings-density`（performance_reports 聚合，§4.2）；
- 空态区分（§4.6/G7）：空数组=正常无事件；接口失败(None)=数据源未接占位。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import add_trading_days

logger = logging.getLogger(__name__)

# 事件扫描窗口 = 自 target_date 起 ≤5 个交易日（§4.6）
HORIZON_TRADING_DAYS = 5


@dataclass
class EventWindow:
    events: list[dict[str, object]] = field(default_factory=list)
    high_events: list[dict[str, object]] = field(default_factory=list)
    source_missing: bool = False
    calendar_uncovered: bool = False


async def load_event_window(
    target_date: str, horizon_days: int = HORIZON_TRADING_DAYS
) -> EventWindow:
    """自 target_date 起 ≤5 个交易日的事件窗口（含 target_date 当日）。

    §16 开放问题 6：交易日历越年 → 同一 fail-close（calendar_uncovered=True），
    禁止各自兜底导致语义分叉。
    """
    try:
        end = add_trading_days(date.fromisoformat(target_date), horizon_days)
    except ValueError:
        logger.warning("event_calendar.calendar_uncovered: target_date=%s", target_date)
        return EventWindow(calendar_uncovered=True)
    date_from, date_to = target_date, end.isoformat()
    raw = await node_api.get_calendar_events(date_from, date_to)
    if raw is None:
        return EventWindow(source_missing=True)
    events: list[dict[str, object]] = list(raw)
    high_events = [e for e in events if e.get("importance") == "high"]
    return EventWindow(events=events, high_events=high_events, source_missing=False)


async def load_earnings_density(date_from: str, date_to: str) -> list[dict[str, object]]:
    """存量披露密度（§4.2，仅存量已公告日，非未来预约，标注口径）。"""
    resp = await node_api.get(
        f"/internal/calendar/earnings-density?dateFrom={date_from}&dateTo={date_to}"
    )
    if not isinstance(resp, dict):
        return []
    density = resp.get("density")
    return list(density) if isinstance(density, list) else []
