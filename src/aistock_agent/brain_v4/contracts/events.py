"""事件契约：现实事件唯一主记录 + 多来源观察记录。

字段口径见设计规格 §3.4。`CanonicalEventRecord` 只保存事实身份、时间、
状态与来源，不保存“利好某板块”这类分析结论。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from . import V4Contract

TimeConfidence = Literal["high", "medium", "low"]
ObservationType = Literal["news", "announcement", "research", "calendar", "crawl"]
EventStatus = Literal["scheduled", "active", "occurred", "corrected", "cancelled", "expired"]


class EventObservation(V4Contract):
    """一条来源观察：新闻、公告、研报、财经日历或抓取结果。"""

    observation_id: str
    event_id: str
    observation_type: ObservationType
    source: str
    source_id: str
    title: str
    published_at: datetime
    event_time: datetime
    fetched_at: datetime
    as_of: datetime
    url: str | None = None
    time_confidence: TimeConfidence
    lifecycle_version: int = Field(ge=1)


class CanonicalEventRecord(V4Contract):
    """一件现实事件的逻辑主记录。"""

    event_id: str
    canonical_type: str
    canonical_title: str
    event_time: datetime
    first_seen_at: datetime
    last_updated_at: datetime
    valid_time: datetime
    status: EventStatus
    source_refs: list[str] = Field(default_factory=list)
    observation_ids: list[str] = Field(default_factory=list)
    entity_refs: list[str] = Field(default_factory=list)
    dedupe_fingerprint: str
    parent_event_id: str | None = None
    supersedes_event_id: str | None = None
    time_confidence: TimeConfidence
    source_conflicts: list[str] = Field(default_factory=list)
    lifecycle_version: int = Field(ge=1)
