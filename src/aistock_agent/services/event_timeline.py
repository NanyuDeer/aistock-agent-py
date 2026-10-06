"""统一事件时间线（Event Entity）→ 节奏大师事件项映射。

- 消费 app-api `/internal/event-entities`（统一时间线，唯一事件权威）；
- 日期口径 = `event_start_time` 的上海日（spec §5.1）；
- `importance` 由本侧确定性分级（spec §3.2；统一时间线无 importance 字段）。
"""
from __future__ import annotations

import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo

from aistock_agent.services.event_calendar import MACRO_EVENT_TERMS

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
    """确定性分级：manual/agent 或标题命中宏观词元 → high；news/calendar/announcement → medium；其余 → low。"""
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
