"""议事层契约：案卷、议程、立场、同伴摘要、信念更新、异议、裁决与进度账本。

字段口径见设计规格 §3.7-§3.18。核心不变量：
- 同伴观点不是证据；`BeliefUpdate` 必须显式记录变化来源；
- `CouncilDecision` 只能比较已有证据，不得创造证据；
- 所有账本 append-only，不得覆盖上一轮。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from . import V4Contract

Materiality = Literal["low", "medium", "high"]
Relation = Literal["confirm", "weaken", "revise", "reverse", "explain", "verify", "carry_forward"]


class CaseFile(V4Contract):
    """议事期间唯一案卷，所有席位读取同一 `case_version`。"""

    case_id: str
    run_id: str
    profile: str
    case_version: int = Field(ge=1)
    question: str
    decision_type: str
    materiality: Materiality
    subject: str
    horizon: str
    as_of: datetime
    snapshot_id: str
    frozen_fact_ids: list[str] = Field(default_factory=list)
    hypothesis_refs: list[str] = Field(default_factory=list)
    open_issue_ids: list[str] = Field(default_factory=list)
    deadline: datetime
    budget_policy: str
    decision_rubric_version: str


class AgendaItem(V4Contract):
    """单个可裁决议题，禁止开放型议题。"""

    issue_id: str
    case_id: str
    question: str
    candidate_hypothesis_refs: list[str] = Field(default_factory=list)
    decision_criteria: list[str] = Field(default_factory=list)
    materiality: Materiality
    owner_role: str
    status: Literal["open", "researching", "deliberating", "resolved", "unresolved", "escalated"]
    opened_at: datetime
    resolved_at: datetime | None = None
    unresolved_reason: str | None = None


class PositionPaper(V4Contract):
    """独立立论。必须至少包含一个可推翻条件。"""

    position_id: str
    issue_id: str
    analyst_role: str
    hypothesis_ref: str
    thesis: str
    mechanism_edge_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    disconfirmers: list[str] = Field(min_length=1)
    unknowns: list[str] = Field(default_factory=list)
    requested_checks: list[str] = Field(default_factory=list)
    created_at: datetime


class Objection(V4Contract):
    """结构化异议，必须指向具体对象并声明击穿条件。"""

    objection_id: str
    issue_id: str
    raised_by: str
    target_type: Literal["position", "claim", "edge", "evidence"]
    target_id: str
    objection_type: Literal[
        "chronology", "provenance", "mechanism", "magnitude", "alternative", "leakage", "scope"
    ]
    severity: Materiality
    argument: str
    required_evidence: list[str] = Field(default_factory=list)
    kill_criterion: str
    status: Literal["open", "answered", "accepted", "unresolved"]


class Rebuttal(V4Contract):
    """对异议的答辩。"""

    rebuttal_id: str
    objection_id: str
    answered_by: str
    response: Literal["accept", "partially_accept", "reject_with_evidence", "request_more_evidence"]
    evidence_ids: list[str] = Field(default_factory=list)
    revision_proposal_id: str | None = None
    residual_uncertainty: str


class RevisionProposal(V4Contract):
    """正式修改提案，只有 RevisionCommitter 能把它转成 RevisionEvent。"""

    proposal_id: str
    issue_id: str
    proposer: str
    target_refs: list[str]
    action: Literal["add_evidence", "reweight", "revise", "split", "merge", "reject"]
    reason: str
    triggering_evidence_ids: list[str] = Field(default_factory=list)
    expected_impact_refs: list[str] = Field(default_factory=list)
    status: Literal["proposed", "accepted", "rejected"]


class CouncilDecision(V4Contract):
    """裁决结果：逐项量表，不做简单多数投票，保留少数意见。"""

    decision_id: str
    issue_id: str
    rubric_version: str
    selected_hypothesis_refs: list[str] = Field(default_factory=list)
    rejected_hypothesis_refs: list[str] = Field(default_factory=list)
    criterion_scores: dict[str, float] = Field(default_factory=dict)
    reasoning_summary: str
    resolved_objection_ids: list[str] = Field(default_factory=list)
    unresolved_objection_ids: list[str] = Field(default_factory=list)
    minority_opinions: list[str] = Field(default_factory=list)
    invalidators: list[str] = Field(default_factory=list)
    decision: Literal["accept", "revise", "insufficient", "escalate"]
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime


class ProgressLedger(V4Contract):
    """run 级编排摘要，串联研究、议事、裁决与回改阶段。"""

    step_no: int = Field(ge=0)
    phase: str
    agenda_status: str
    has_progress: bool
    new_evidence_count: int = Field(ge=0)
    changed_ranking: bool
    open_high_objection_ids: list[str] = Field(default_factory=list)
    next_role: str | None = None
    next_instruction: str | None = None
    stall_count: int = Field(ge=0)
    plan_reset_count: int = Field(ge=0)
    budget_remaining: int = Field(ge=0)
    stop_reason: str | None = None


class CollaborationDecision(V4Contract):
    """确定性路由结果。模型建议写入 `suggested_mode`，不得覆盖 `mode`。"""

    mode: Literal["SOLO_DEEP", "LEAD_WITH_CONSULTANTS", "ANALYST_COUNCIL", "MANDATORY_RED_TEAM"]
    suggested_mode: str | None = None
    trigger_codes: list[str] = Field(default_factory=list)
    selected_roles: list[str] = Field(default_factory=list)
    lead_role: str
    judge_role: str
    max_objection_rounds: int = Field(ge=0)
    max_rebuttal_rounds: int = Field(ge=0)
    human_required: bool


class PeerBrief(V4Contract):
    """控制器裁剪后的最小必要同伴摘要，首轮独立立论前禁止生成。"""

    peer_brief_id: str
    case_id: str
    case_version: int = Field(ge=1)
    deliberation_round: int = Field(ge=0)
    target_role: str
    source_artifact_ids: list[str] = Field(default_factory=list)
    competing_claims: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    material_disagreements: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    omitted_sections: list[str] = Field(default_factory=list)
    generated_at: datetime


class BeliefUpdate(V4Contract):
    """席位观点变化记录，即使 hold 也必须提交。"""

    belief_update_id: str
    issue_id: str
    analyst_role: str
    case_version: int = Field(ge=1)
    deliberation_round: int = Field(ge=0)
    prior_hypothesis_ref: str
    prior_confidence: float = Field(ge=0.0, le=1.0)
    action: Literal["hold", "strengthen", "weaken", "switch", "split", "merge"]
    posterior_hypothesis_refs: list[str] = Field(default_factory=list)
    posterior_confidence: list[float] = Field(default_factory=list)
    influencing_artifact_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    changed_assumptions: list[str] = Field(default_factory=list)
    requested_gap_ids: list[str] = Field(default_factory=list)
    reason_summary: str


class InfluenceEdge(V4Contract):
    """“谁的哪个产物改变了谁的哪条结论”。"""

    influence_edge_id: str
    source_artifact_id: str
    target_belief_update_id: str
    target_hypothesis_ref: str
    influence_type: Literal[
        "search_direction",
        "assumption",
        "mechanism",
        "confidence",
        "hypothesis_topology",
        "objection",
    ]
    effect: Literal["reinforce", "weaken", "redirect", "split", "merge", "reject"]
    evidence_backed: bool
    materiality: Materiality
    created_at: datetime


class DeliberationRoundLedger(V4Contract):
    """议事轮账本；案卷升级后只重跑 `affected_role_ids`。"""

    deliberation_round: int = Field(ge=0)
    case_version: int = Field(ge=1)
    active_issue_ids: list[str] = Field(default_factory=list)
    peer_brief_ids: list[str] = Field(default_factory=list)
    belief_update_ids: list[str] = Field(default_factory=list)
    influence_edge_ids: list[str] = Field(default_factory=list)
    new_evidence_count: int = Field(ge=0)
    closed_high_objection_ids: list[str] = Field(default_factory=list)
    max_belief_delta: float
    ranking_changed: bool
    topology_changed: bool
    affected_role_ids: list[str] = Field(default_factory=list)
    next_action: str
    stop_reason: str | None = None
