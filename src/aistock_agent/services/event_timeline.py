"""统一事件时间线（Event Entity）→ 节奏大师事件项映射。

- 消费 app-api `/internal/event-entities`（统一时间线，唯一事件权威）；
- 日期口径 = `event_start_time` 的上海日（spec §5.1）；
- `importance` 由本侧确定性分级（spec §3.2；统一时间线无 importance 字段）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_calendar import (
    HORIZON_TRADING_DAYS,
    MACRO_EVENT_TERMS,
    EventWindow,
)
from aistock_agent.utils.date import (
    CALENDAR_MAX_YEAR,
    CALENDAR_MIN_YEAR,
    add_trading_days,
)

logger = logging.getLogger(__name__)

SHANGHAI = ZoneInfo("Asia/Shanghai")

# source_type → 节奏大师既有事件类型域（对齐前端 eventTypeLabel）
SOURCE_TYPE_TO_EVENT_TYPE: dict[str, str] = {
    "calendar": "macro",
    "announcement": "earnings",
    "news": "seed",
    "manual": "seed",
    "agent": "seed",
}


def shanghai_start_date(event_start_time: object) -> str | None:
    """`event_start_time` → 上海日 `YYYY-MM-DD`；无法解析 → None（不抛异常）。"""
    if not isinstance(event_start_time, str):
        return None
    raw = event_start_time.strip()
    if not raw:
        return None
    try:
        if len(raw) == 10:  # date-only
            return date.fromisoformat(raw).isoformat()
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=SHANGHAI)
        return dt.astimezone(SHANGHAI).date().isoformat()
    except ValueError:
        return None


def grade_importance(entity: dict[str, object]) -> str:
    """确定性分级：manual/agent 或标题命中宏观词元 → high；
    news/calendar/announcement → medium；其余 → low。"""
    title = str(entity.get("title") or "")
    source_type = str(entity.get("source_type") or "")
    if source_type in {"manual", "agent"}:
        return "high"
    if any(term in title for term in MACRO_EVENT_TERMS):
        return "high"
    if source_type in {"news", "calendar", "announcement"}:
        return "medium"
    return "low"


def to_timeline_event(entity: dict[str, object]) -> dict[str, object] | None:
    """单条实体 → 节奏事件项；日期/标题缺失 → None（跳过）。"""
    date_str = shanghai_start_date(entity.get("event_start_time"))
    title = str(entity.get("title") or "").strip()
    if not date_str or not title:
        return None
    source_type = str(entity.get("source_type") or "")
    return {
        "date": date_str,
        "type": SOURCE_TYPE_TO_EVENT_TYPE.get(source_type, "seed"),
        "title": title,
        "importance": grade_importance(entity),
    }


_HORIZON_DISPLAY_YEAR_END_MONTH = 12
_HORIZON_DISPLAY_YEAR_END_DAY = 31


def _merge_events(
    schedules: list[dict[str, object]], events: list[dict[str, object]]
) -> list[dict[str, object]]:
    """规则日程在前 + 时间线事件在后；按 (date,title) 去重保留首次；按 date 稳定排序。"""
    seen: set[tuple[str, str]] = set()
    out: list[dict[str, object]] = []
    for e in [*schedules, *events]:
        key = (str(e.get("date") or ""), str(e.get("title") or ""))
        if not key[0] or not key[1] or key in seen:
            continue
        seen.add(key)
        out.append(e)
    out.sort(key=lambda e: str(e.get("date") or ""))
    return out


async def load_event_timeline(
    target_date: str, horizon_days: int | None = HORIZON_TRADING_DAYS
) -> EventWindow:
    """统一时间线事件窗口。

    - 事件 = `GET /internal/event-entities`（按 event_start_time 上海日过滤，含未来事件）；
    - 日程 = `GET /internal/calendar/events` 中 `source == "L1"` 的规则交割日（合成补充项）；
    - 越年（超出 CALENDAR_MIN_YEAR..MAX_YEAR）→ fail-close（calendar_uncovered）。
    """
    target = date.fromisoformat(target_date)
    if not CALENDAR_MIN_YEAR <= target.year <= CALENDAR_MAX_YEAR:
        logger.warning("event_timeline.calendar_uncovered target_date=%s", target_date)
        return EventWindow(calendar_uncovered=True)
    if horizon_days is None:
        end = date(
            CALENDAR_MAX_YEAR,
            _HORIZON_DISPLAY_YEAR_END_MONTH,
            _HORIZON_DISPLAY_YEAR_END_DAY,
        )
    else:
        try:
            end = add_trading_days(target, horizon_days)
        except ValueError:
            logger.warning("event_timeline.calendar_uncovered target_date=%s", target_date)
            return EventWindow(calendar_uncovered=True)
    date_from, date_to = target_date, end.isoformat()

    entities = await node_api.get_event_entities({"dateFrom": date_from, "dateTo": date_to})
    if entities is None:
        return EventWindow(source_missing=True)
    timeline_events = [ev for ev in (to_timeline_event(e) for e in entities) if ev]

    raw_calendar = await node_api.get_calendar_events(date_from, date_to)
    l1_schedules = [
        e for e in (raw_calendar or []) if str(e.get("source") or "") == "L1"
    ]

    merged = _merge_events(l1_schedules, timeline_events)
    high = [e for e in merged if e.get("importance") == "high"]
    win = EventWindow(events=merged, high_events=high, source_missing=False)
    if horizon_days is None:
        win.display_events = merged
    return win
