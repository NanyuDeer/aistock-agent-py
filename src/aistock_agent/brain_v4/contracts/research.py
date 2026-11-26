"""研究层契约：请求、假设、因果边、缺口、回改事件与轮次账本。

字段口径见 docs/superpowers/specs/2026-11-26-smart-brain-v4-design.md §3.1-§3.6。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from . import V4Contract

HypothesisStatus = Literal["open", "supported", "weakened", "rejected", "frozen"]
EdgeType = Literal["fact", "inference", "hypothesis"]
GapType = Literal[
    "missing_evidence",
    "time_order",
    "mechanism",
    "magnitude",
    "alternative",
    "invalidator",
    "edge_support",
]
RevisionAction = Literal["add_evidence", "reweight", "revise", "split", "merge", "reject"]


class ResearchRequest(V4Contract):
    """一次研究运行的输入边界。

    `as_of` / `data_cutoff` / `snapshot_id` 是硬约束：缺少它们必须报错，
    节点也不得用系统当前时间代替。
    """

    request_id: str
    run_id: str
    profile: str
    subject_type: str
    subject_code: str
    research_goal: str
    required_questions: list[str] = Field(default_factory=list)
    horizon: str
    as_of: datetime
    data_cutoff: datetime
    snapshot_id: str
    config_version: str
    model_version: str
    prompt_version: str
    method_version: str
    budget_policy: str
    stop_policy: str


class Hypothesis(V4Contract):
    """可版本化的研究假设。

    修改假设必须新增版本，旧版本不可覆盖，便于回溯“何时因何证据改口”。
    """

    hypothesis_id: str
    version: int = Field(ge=1)
    statement: str
    status: HypothesisStatus
    prior_confidence: float = Field(ge=0.0, le=1.0)
    assumptions: list[str] = Field(default_factory=list)
    disconfirmers: list[str] = Field(min_length=1)
    required_evidence_types: list[str] = Field(default_factory=list)
    parent_ids: list[str] = Field(default_factory=list)
    scope: str

    @property
    def ref(self) -> str:
        """稳定引用：`hypothesis_id:v{version}`。"""
        return f"{self.hypothesis_id}:v{self.version}"


class CausalEdge(V4Contract):
    """因果边。没有证据的边只能标为 hypothesis，不得进入高置信主因权重。"""

    edge_id: str
    from_node: str
    to_node: str
    mechanism: str
    supporting_evidence_ids: list[str] = Field(default_factory=list)
    contradicting_evidence_ids: list[str] = Field(default_factory=list)
    edge_type: EdgeType
    confidence: float = Field(ge=0.0, le=1.0)
    valid_time: datetime
    magnitude_check: str
    counterfactual_check: str


class ResearchGap(V4Contract):
    """结构化的研究缺口，由充分性门禁产出，供定向补证使用。"""

    gap_id: str
    run_id: str
    issue_id: str
    gap_type: GapType
    description: str
    required_evidence_types: list[str] = Field(default_factory=list)
    target_hypothesis_refs: list[str] = Field(default_factory=list)
    materiality: Literal["low", "medium", "high"]
    status: Literal["open", "researching", "resolved", "abandoned"]


class RevisionEvent(V4Contract):
    """正式回改记录：必须能回答“哪条新证据使哪条旧结论失效”。"""

    revision_id: str
    run_id: str
    action: RevisionAction
    target_refs: list[str]
    triggering_evidence_ids: list[str]
    impacted_hypothesis_refs: list[str] = Field(default_factory=list)
    impacted_claim_ids: list[str] = Field(default_factory=list)
    reason: str
    case_version: int = Field(ge=1)
    created_at: datetime


class RoundLedger(V4Contract):
    """研究轮账本，append-only，防止上一轮判断被下一轮覆盖。"""

    round_no: int = Field(ge=0)
    new_evidence_ids: list[str] = Field(default_factory=list)
    changed_hypothesis_ids: list[str] = Field(default_factory=list)
    unresolved_gaps: list[str] = Field(default_factory=list)
    contradictions: list[str] = Field(default_factory=list)
    next_action: str
    progress_score: float = Field(ge=0.0, le=1.0)
    stall_count: int = Field(ge=0)
    revision_budget_left: int = Field(ge=0)
    stop_reason: str | None = None
