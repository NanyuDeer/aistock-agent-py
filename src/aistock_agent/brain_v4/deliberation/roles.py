"""确定性席位表、角色上下文与立论桩。

职责边界（设计规格 §2.7 / §2.8）：
- 席位选择由 profile/mode 决定，不允许模型自由加席；
- `build_role_context` 只暴露公共产物；R0 强制隐藏一切同伴摘要；
- `run_role_node` 是 Task 6 LLM 内核的确定性替身，只返回
  `submissions` 与 `usage`，绝不写 `hypothesis_graph` / `causal_graph`。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.deliberation import (
    AgendaItem,
    BeliefUpdate,
    CaseFile,
    PeerBrief,
    PositionPaper,
)
from ..contracts.evidence import EvidenceLedger
from ..contracts.research import Hypothesis

LEAD_ROLE = "lead_thesis"
JUDGE_ROLE = "council_judge"
RED_TEAM_ROLE = "red_team"

#: 每种协作模式的确定性席位表；未列出的模式显式报错，避免静默使用默认席位。
_SEAT_TABLE: dict[str, tuple[str, ...]] = {
    "SOLO_DEEP": (),
    "LEAD_WITH_CONSULTANTS": ("mechanism", "quant"),
    "ANALYST_COUNCIL": ("lead_thesis", "alternative", "timeline", "mechanism", "quant"),
    "MANDATORY_RED_TEAM": (
        "lead_thesis",
        "alternative",
        "timeline",
        "mechanism",
        "quant",
        "red_team",
    ),
}

#: 各席位选择候选假设的下标（按置信度降序、同分按 ref 排序）。
_ROLE_HYPOTHESIS_INDEX: dict[str, int] = {
    "lead_thesis": 0,
    "timeline": 0,
    "quant": 0,
    "alternative": 1,
    "mechanism": 1,
    "red_team": 1,
}

_TIMELINE_UNKNOWNS: tuple[str, ...] = ("gap_pre_move_trigger", "timeline_conflict")


class RoleContext(V4Contract):
    """单个席位可见的最小案卷投影。"""

    role: str
    round_no: int = Field(ge=0)
    case_version: int = Field(ge=1)
    case_file: CaseFile
    agenda_items: list[AgendaItem] = Field(default_factory=list)
    public_positions: list[PositionPaper] = Field(default_factory=list)
    peer_briefs: list[PeerBrief] = Field(default_factory=list)
    belief_updates: list[BeliefUpdate] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


def select_roles(mode: str, *, cross_layer_conflict: bool = False) -> list[str]:
    """按协作模式返回席位；跨层冲突在议会模式下强制加入时间线审计。"""
    try:
        roles = list(_SEAT_TABLE[mode])
    except KeyError as exc:
        raise KeyError(f"unknown collaboration mode: {mode!r}") from exc
    if mode == "ANALYST_COUNCIL" and cross_layer_conflict and "timeline" not in roles:
        roles.append("timeline")
    return sorted(roles)


def _value(state: Mapping[str, Any], key: str, default: Any) -> Any:
    return state.get(key, default)


def build_role_context(
    *, round_no: int, role: str, state: Mapping[str, Any]
) -> RoleContext:
    """构造席位上下文；R0 不暴露任何同伴立场与摘要。"""
    case_file = _value(state, "case_file", None)
    if not isinstance(case_file, CaseFile):
        raise ValueError("role context requires a CaseFile in state")

    submissions = list(_value(state, "submissions", ()))
    all_briefs = list(_value(state, "peer_briefs", ()))
    belief_updates = list(_value(state, "belief_updates", ()))

    if round_no == 0:
        visible_positions: list[PositionPaper] = []
        visible_briefs: list[PeerBrief] = []
    else:
        visible_positions = [paper for paper in submissions if paper.analyst_role != role]
        visible_briefs = [
            brief
            for brief in all_briefs
            if brief.target_role == role and brief.deliberation_round == round_no
        ]

    ledger = _value(state, "evidence_ledger", None)
    evidence_ids = (
        sorted(item.evidence_id for item in ledger.items)
        if isinstance(ledger, EvidenceLedger)
        else []
    )

    return RoleContext(
        role=role,
        round_no=round_no,
        case_version=int(_value(state, "case_version", case_file.case_version)),
        case_file=case_file,
        agenda_items=list(_value(state, "agenda_items", ())),
        public_positions=sorted(visible_positions, key=lambda item: item.position_id),
        peer_briefs=sorted(visible_briefs, key=lambda item: item.peer_brief_id),
        belief_updates=sorted(
            belief_updates, key=lambda item: (item.deliberation_round, item.belief_update_id)
        ),
        evidence_ids=evidence_ids,
    )


def _ordered_hypotheses(hypotheses: Sequence[Hypothesis]) -> list[Hypothesis]:
    return sorted(hypotheses, key=lambda item: (-item.prior_confidence, item.ref))


def _choose_hypothesis(role: str, hypotheses: Sequence[Hypothesis]) -> Hypothesis:
    ordered = _ordered_hypotheses(hypotheses)
    if not ordered:
        raise ValueError("run_role_node requires at least one hypothesis")
    index = _ROLE_HYPOTHESIS_INDEX.get(role, 0)
    if index >= len(ordered):
        index = 0
    return ordered[index]


def _latest_belief_confidence(
    state: Mapping[str, Any], role: str, hypothesis_ref: str
) -> float | None:
    beliefs = [
        item
        for item in state.get("belief_updates", ())
        if item.analyst_role == role and item.prior_hypothesis_ref == hypothesis_ref
    ]
    if not beliefs:
        return None
    latest = sorted(
        beliefs, key=lambda item: (item.deliberation_round, item.belief_update_id)
    )[-1]
    for ref, confidence in zip(
        latest.posterior_hypothesis_refs, latest.posterior_confidence, strict=False
    ):
        if ref == hypothesis_ref:
            return float(confidence)
    return None


def _issue_id_for(hypothesis_ref: str, agenda_items: Sequence[AgendaItem]) -> str:
    for item in agenda_items:
        if hypothesis_ref in item.candidate_hypothesis_refs:
            return item.issue_id
    return f"issue_{hypothesis_ref.split(':', 1)[0]}"


def run_role_node(role: str, state: Mapping[str, Any]) -> dict[str, Any]:
    """确定性立论桩：只产出 `PositionPaper` 与 usage，不触碰规范图。"""
    case_file = _value(state, "case_file", None)
    if not isinstance(case_file, CaseFile):
        raise ValueError("run_role_node requires a CaseFile in state")
    hypotheses = list(_value(state, "hypotheses", ()))
    hypothesis = _choose_hypothesis(role, hypotheses)

    round_no = int(_value(state, "round_no", 0))
    case_version = int(_value(state, "case_version", case_file.case_version))
    agenda_items = list(_value(state, "agenda_items", ()))

    ledger = _value(state, "evidence_ledger", None)
    evidence_ids = (
        sorted(
            item.evidence_id
            for item in ledger.items
            if hypothesis.ref in item.supports_hypothesis_refs
        )
        if isinstance(ledger, EvidenceLedger)
        else []
    )

    posterior = _latest_belief_confidence(state, role, hypothesis.ref)
    confidence = posterior if posterior is not None else hypothesis.prior_confidence
    unknowns = list(_TIMELINE_UNKNOWNS) if role == "timeline" else []

    paper = PositionPaper(
        position_id=(
            f"pos_r{round_no:02d}_v{case_version:02d}_{role}_{hypothesis.hypothesis_id}"
        ),
        issue_id=_issue_id_for(hypothesis.ref, agenda_items),
        analyst_role=role,
        hypothesis_ref=hypothesis.ref,
        thesis=f"{role} 支持 {hypothesis.ref}：{hypothesis.statement}",
        evidence_ids=evidence_ids,
        assumptions=hypothesis.assumptions,
        confidence=confidence,
        disconfirmers=hypothesis.disconfirmers,
        unknowns=unknowns,
        created_at=case_file.as_of,
    )
    return {
        "submissions": [paper],
        "usage": [{"key": "steps", "value": 1}],
    }
