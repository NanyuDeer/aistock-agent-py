"""V4 规范状态与确定性 reducer。

分工（设计规格 §2.8）：
- 并行节点只能追加 `submissions / objections / belief_updates / influence_edges / usage / errors`；
- `hypothesis_graph / causal_graph / council_decision / judgment_draft` 必须单写，不提供 reducer。

确定性要求：同一批记录无论到达顺序如何，合并结果逐字节一致；
同一 ID 内容不同时显式产出 conflict，不允许“最后到达者覆盖”。
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, TypedDict

from pydantic import BaseModel, Field

from ..contracts.deliberation import (
    AgendaItem,
    BeliefUpdate,
    CaseFile,
    CollaborationDecision,
    CouncilDecision,
    DeliberationRoundLedger,
    InfluenceEdge,
    Objection,
    PeerBrief,
    PositionPaper,
    ProgressLedger,
    Rebuttal,
    RevisionProposal,
)
from ..contracts.evidence import EvidenceLedger
from ..contracts.judgment import V4Judgment
from ..contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchGap,
    ResearchRequest,
    RevisionEvent,
    RoundLedger,
)

#: 并行节点可追加的通道
PARALLEL_APPEND_CHANNELS: frozenset[str] = frozenset(
    {"submissions", "objections", "belief_updates", "influence_edges", "usage", "errors"}
)

#: 必须单写的规范图与决策通道，禁止挂 reducer
SINGLE_WRITER_CHANNELS: frozenset[str] = frozenset(
    {"hypothesis_graph", "causal_graph", "council_decision", "judgment_draft"}
)


class BrainV4State(TypedDict, total=False):
    """V4 运行状态。规范字段可回放，私有 scratch 不进入本状态。"""

    request: ResearchRequest
    case_file: CaseFile
    evidence_ledger: EvidenceLedger
    hypotheses: list[Hypothesis]
    hypothesis_graph: dict[str, Hypothesis]
    causal_graph: list[CausalEdge]
    round_ledgers: list[RoundLedger]
    research_gaps: list[ResearchGap]
    revision_events: list[RevisionEvent]
    submissions: list[PositionPaper]
    objections: list[Objection]
    rebuttals: list[Rebuttal]
    peer_briefs: list[PeerBrief]
    belief_updates: list[BeliefUpdate]
    influence_edges: list[InfluenceEdge]
    revision_proposals: list[RevisionProposal]
    agenda_items: list[AgendaItem]
    deliberation_round_ledgers: list[DeliberationRoundLedger]
    council_decision: CouncilDecision
    collaboration_decision: CollaborationDecision
    progress_ledger: ProgressLedger
    judgment_draft: V4Judgment
    usage: list[dict[str, Any]]
    errors: list[dict[str, Any]]
    conflicts: list[dict[str, Any]]
    run_status: str
    stop_reason: str
    trace: list[dict[str, Any]]


class MergeConflict(BaseModel):
    """同一业务 ID 出现不同内容时记录冲突，保留确定性胜者。"""

    record_id: str
    kept_digest: str
    dropped_digests: list[str] = Field(default_factory=list)


class MergeResult(BaseModel):
    """合并结果：稳定排序后的记录与被显式保留的冲突。"""

    items: list[dict[str, Any]] = Field(default_factory=list)
    conflicts: list[MergeConflict] = Field(default_factory=list)


def canonical_digest(payload: Any) -> str:
    """对任意载荷做规范化 JSON 摘要，用于顺序无关比较。"""
    encoded = json.dumps(
        payload, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":")
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def merge_by_id(
    current: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    id_field: str,
) -> MergeResult:
    """按业务 ID 合并两组记录，结果与到达顺序无关。

    同 ID 同内容去重；同 ID 不同内容时保留 `canonical_digest` 较小者，
    并输出 `MergeConflict`，绝不采用“最后到达者覆盖”。
    """
    kept: dict[str, dict[str, Any]] = {}
    kept_digests: dict[str, str] = {}
    dropped: dict[str, set[str]] = {}

    for record in (*current, *incoming):
        record_id = str(record[id_field])
        record_dict = dict(record)
        digest = canonical_digest(record_dict)
        existing_digest = kept_digests.get(record_id)
        if existing_digest is None:
            kept[record_id] = record_dict
            kept_digests[record_id] = digest
            continue
        if existing_digest == digest:
            continue
        if digest < existing_digest:
            kept[record_id] = record_dict
            kept_digests[record_id] = digest
            dropped.setdefault(record_id, set()).add(existing_digest)
        else:
            dropped.setdefault(record_id, set()).add(digest)

    conflicts = [
        MergeConflict(
            record_id=record_id,
            kept_digest=kept_digests[record_id],
            dropped_digests=sorted(digests),
        )
        for record_id, digests in sorted(dropped.items())
    ]
    items = [kept[record_id] for record_id in sorted(kept)]
    return MergeResult(items=items, conflicts=conflicts)


def append_events(
    current: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
    id_field: str,
) -> list[dict[str, Any]]:
    """追加通道：按 ID 去重并稳定排序，不覆盖成功记录。"""
    return merge_by_id(current, incoming, id_field).items


def sum_usage(
    current: Sequence[Mapping[str, Any]],
    incoming: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """usage 通道：同 key 数值相加，输出按 key 稳定排序。"""
    totals: dict[str, int | float] = {}
    for record in (*current, *incoming):
        key = str(record["key"])
        value = record["value"]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise TypeError(f"usage value for {key!r} must be numeric, got {type(value).__name__}")
        totals[key] = totals.get(key, 0) + value
    return [{"key": key, "value": totals[key]} for key in sorted(totals)]
