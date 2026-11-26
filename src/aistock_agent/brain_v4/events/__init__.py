"""V4 事件层：旧事件来源到 canonical event 的适配。

`adapters.py` 把 agent `event_store`、app-api `event_entities` 与 calendar 行
映射到同一 `CanonicalEventRecord` + 多条 `EventObservation` 契约。
"""

from .adapters import (
    adapt_agent_event,
    adapt_calendar_row,
    adapt_event_entity,
    can_mark_initial_trigger,
    dedupe_fingerprint,
    normalize_event_title,
    resolve_canonical_events,
)

__all__ = [
    "adapt_agent_event",
    "adapt_calendar_row",
    "adapt_event_entity",
    "can_mark_initial_trigger",
    "dedupe_fingerprint",
    "normalize_event_title",
    "resolve_canonical_events",
]
