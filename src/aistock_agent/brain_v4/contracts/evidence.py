"""证据层契约：证据条目与证据账本。

字段口径见设计规格 §3.3：来源、三时间（事件/发布/抓取）、as_of、
证据类型、质量、时间置信度、重复与冲突关系、工具与参数摘要。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from . import V4Contract

EvidenceKind = Literal["direct", "indirect", "model_inference"]
QualityLevel = Literal["high", "medium", "low"]


class EvidenceItem(V4Contract):
    """一条可追溯证据。"""

    evidence_id: str
    source: str
    source_type: str
    event_time: datetime
    published_at: datetime
    fetched_at: datetime
    as_of: datetime
    evidence_kind: EvidenceKind
    supports_hypothesis_refs: list[str] = Field(default_factory=list)
    contradicts_hypothesis_refs: list[str] = Field(default_factory=list)
    data_quality: QualityLevel
    time_confidence: QualityLevel
    duplicate_of: str | None = None
    conflicts_with: list[str] = Field(default_factory=list)
    tool: str
    tool_params: dict[str, str | int | float | bool] = Field(default_factory=dict)
    response_digest: str
    snapshot_id: str


class EvidenceLedger(V4Contract):
    """一次 run 的证据账本，冻结后只允许追加。"""

    run_id: str
    snapshot_id: str
    as_of: datetime
    items: list[EvidenceItem] = Field(default_factory=list)
    conflict_ids: list[str] = Field(default_factory=list)
