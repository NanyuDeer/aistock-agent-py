"""旧事件来源到 `CanonicalEventRecord` 的适配器。

三个来源（agent `event_store`、app-api `event_entities`、calendar 行）先各自
映射为 `EventObservation`，再由 `resolve_canonical_events` 按 dedupe 指纹聚合为
一件现实事件：同一事件只有一个 `event_id`，多来源保留多条 observation；
更正通过递增 `lifecycle_version` 体现，冲突显式记录在 `source_conflicts`。
"""

import hashlib
import unicodedata
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from ..contracts.events import (
    CanonicalEventRecord,
    EventObservation,
    EventStatus,
    ObservationType,
    TimeConfidence,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")

#: app-api `event_entities.source_type` → 观察类型。
_SOURCE_TYPE_TO_OBSERVATION: dict[str, ObservationType] = {
    "news": "news",
    "announcement": "announcement",
    "calendar": "calendar",
    "manual": "crawl",
    "agent": "crawl",
}

#: canonical_type 的稳定优先级（announcement 最权威）。
_TYPE_PRECEDENCE: tuple[ObservationType, ...] = (
    "announcement",
    "news",
    "research",
    "calendar",
    "crawl",
)

_CONFIDENCE_RANK: dict[TimeConfidence, int] = {"high": 2, "medium": 1, "low": 0}


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def normalize_event_title(title: str) -> str:
    """事件标题轻归一：NFKC + 去空白/标点/符号 + 小写。

    只去书写噪声，不做语义归并（"2026-09" 与 "9月" 不等价）。
    """
    text = unicodedata.normalize("NFKC", title)
    kept: list[str] = []
    for char in text.lower():
        if char.isspace() or char == "_":
            continue
        if unicodedata.category(char)[0] in {"P", "S"}:
            continue
        kept.append(char)
    return "".join(kept)


def dedupe_fingerprint(title: str, event_time: datetime) -> str:
    """事件去重指纹：上海时区事件日 + 归一标题的 SHA-256。"""
    event_day = _aware(event_time).astimezone(_SHANGHAI).date().isoformat()
    payload = f"{event_day}|{normalize_event_title(title)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _observation_id(
    observation_type: ObservationType,
    source: str,
    source_id: str,
    published_at: datetime,
    lifecycle_version: int,
) -> str:
    payload = (
        f"{observation_type}|{source}|{source_id}|"
        f"{_aware(published_at).isoformat()}|v{lifecycle_version}"
    )
    return f"obs_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:20]}"


def _confidence_from_score(score: float | None) -> TimeConfidence:
    if score is None:
        return "medium"
    if score >= 0.8:
        return "high"
    if score >= 0.5:
        return "medium"
    return "low"


def adapt_agent_event(
    row: Mapping[str, Any],
    *,
    as_of: datetime,
    fetched_at: datetime,
    lifecycle_version: int = 1,
) -> EventObservation:
    """agent `event_store` 行 → news observation。"""
    source = str(row.get("source") or "event_store")
    source_id = str(row.get("event_id") or row.get("source_event_id") or "")
    title = str(row.get("title") or "")
    scrape_at = _parse_dt(row.get("scrape_at")) or fetched_at
    score_date = row.get("score_date")
    event_time = _parse_dt(f"{score_date}T00:00:00+08:00") if isinstance(score_date, str) and score_date else None
    event_time = event_time or scrape_at
    fingerprint = dedupe_fingerprint(title, event_time)
    url = row.get("url")
    return EventObservation(
        observation_id=_observation_id("news", source, source_id, scrape_at, lifecycle_version),
        event_id=f"evt_{fingerprint}",
        observation_type="news",
        source=source,
        source_id=source_id,
        title=title,
        published_at=scrape_at,
        event_time=event_time,
        fetched_at=fetched_at,
        as_of=as_of,
        url=url if isinstance(url, str) else None,
        time_confidence="medium",
        lifecycle_version=lifecycle_version,
    )


def adapt_event_entity(
    row: Mapping[str, Any],
    *,
    as_of: datetime,
    fetched_at: datetime,
    lifecycle_version: int = 1,
) -> EventObservation:
    """app-api `event_entities` 行 → observation。"""
    source_id = str(row.get("event_id") or "")
    title = str(row.get("title") or "")
    source_type = str(row.get("source_type") or "news")
    observation_type: ObservationType = _SOURCE_TYPE_TO_OBSERVATION.get(source_type, "news")
    event_time = _parse_dt(row.get("event_start_time")) or fetched_at
    published_at = (
        _parse_dt(row.get("publish_time"))
        or _parse_dt(row.get("scrape_at"))
        or event_time
    )
    raw_confidence = row.get("time_confidence")
    score = (
        float(raw_confidence)
        if isinstance(raw_confidence, int | float) and not isinstance(raw_confidence, bool)
        else None
    )
    fingerprint = dedupe_fingerprint(title, event_time)
    return EventObservation(
        observation_id=_observation_id(
            observation_type, "event_entities", source_id, published_at, lifecycle_version
        ),
        event_id=f"evt_{fingerprint}",
        observation_type=observation_type,
        source="event_entities",
        source_id=source_id,
        title=title,
        published_at=published_at,
        event_time=event_time,
        fetched_at=fetched_at,
        as_of=as_of,
        url=None,
        time_confidence=_confidence_from_score(score),
        lifecycle_version=lifecycle_version,
    )


def adapt_calendar_row(
    row: Mapping[str, Any],
    *,
    as_of: datetime,
    fetched_at: datetime,
    lifecycle_version: int = 1,
) -> EventObservation:
    """`market_calendar_events` 行 → calendar observation。"""
    event_date = str(row.get("event_date") or "")
    raw_time = str(row.get("event_time") or "00:00")
    time_text = raw_time if raw_time.count(":") == 2 else f"{raw_time}:00"
    event_time = _parse_dt(f"{event_date}T{time_text}+08:00")
    if event_time is None:
        raise ValueError(f"calendar row missing valid event_date: {event_date!r}")
    title = str(row.get("title") or "")
    source = str(row.get("source") or "calendar")
    source_id = f"{event_date}|{title}"
    fingerprint = dedupe_fingerprint(title, event_time)
    return EventObservation(
        observation_id=_observation_id("calendar", source, source_id, event_time, lifecycle_version),
        event_id=f"evt_{fingerprint}",
        observation_type="calendar",
        source=source,
        source_id=source_id,
        title=title,
        published_at=event_time,
        event_time=event_time,
        fetched_at=fetched_at,
        as_of=as_of,
        url=None,
        time_confidence="high",
        lifecycle_version=lifecycle_version,
    )


def resolve_canonical_events(
    observations: Sequence[EventObservation],
) -> list[CanonicalEventRecord]:
    """按 `event_id`（dedupe 指纹）聚合观察记录，输出稳定排序的 canonical 记录。"""
    groups: dict[str, list[EventObservation]] = {}
    for observation in observations:
        groups.setdefault(observation.event_id, []).append(observation)
    return [_build_record(event_id, _ordered(groups[event_id])) for event_id in sorted(groups)]


def _ordered(group: Sequence[EventObservation]) -> list[EventObservation]:
    return sorted(group, key=lambda item: (_aware(item.published_at), item.observation_id))


def _build_record(event_id: str, ordered: Sequence[EventObservation]) -> CanonicalEventRecord:
    latest = ordered[-1]
    reference_as_of = max(_aware(item.as_of) for item in ordered)
    max_lifecycle = max(item.lifecycle_version for item in ordered)
    observed_types = {item.observation_type for item in ordered}
    canonical_type = next(kind for kind in _TYPE_PRECEDENCE if kind in observed_types)

    status: EventStatus
    if max_lifecycle > 1:
        status = "corrected"
    elif _aware(latest.event_time) > reference_as_of:
        status = "scheduled"
    else:
        status = "occurred"

    confidence: TimeConfidence = min(
        (item.time_confidence for item in ordered),
        key=lambda value: _CONFIDENCE_RANK[value],
    )
    return CanonicalEventRecord(
        event_id=event_id,
        canonical_type=canonical_type,
        canonical_title=latest.title,
        event_time=latest.event_time,
        first_seen_at=min(_aware(item.fetched_at) for item in ordered),
        last_updated_at=max(_aware(item.fetched_at) for item in ordered),
        valid_time=latest.event_time,
        status=status,
        source_refs=sorted({f"{item.source}:{item.source_id}" for item in ordered}),
        observation_ids=sorted(item.observation_id for item in ordered),
        entity_refs=[],
        dedupe_fingerprint=_fingerprint_for(event_id, latest.title, latest.event_time),
        parent_event_id=None,
        supersedes_event_id=None,
        time_confidence=confidence,
        source_conflicts=_conflicts(ordered),
        lifecycle_version=max_lifecycle,
    )


def _fingerprint_for(event_id: str, title: str, event_time: datetime) -> str:
    if event_id.startswith("evt_") and len(event_id) > len("evt_"):
        return event_id[len("evt_") :]
    return dedupe_fingerprint(title, event_time)


def _conflicts(group: Sequence[EventObservation]) -> list[str]:
    conflicts: list[str] = []
    if len({normalize_event_title(item.title) for item in group}) > 1:
        conflicts.append("title_conflict")
    if len({_aware(item.event_time).isoformat() for item in group}) > 1:
        conflicts.append("event_time_conflict")
    return conflicts


def can_mark_initial_trigger(observation: EventObservation, price_start_at: datetime) -> bool:
    """发布时间晚于价格起点时不得标为初始触发（避免未来信息泄漏）。"""
    return not (_aware(observation.published_at) > _aware(price_start_at))
