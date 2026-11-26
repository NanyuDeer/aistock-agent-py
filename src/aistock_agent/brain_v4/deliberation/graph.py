"""案卷制议会多轮 StateGraph。

路径（设计规格 §2.6）：
`case_freeze -> agenda_build -> r0_independent_positions -> fact_normalize
 -> peer_brief_reveal -> cross_exam -> belief_update -> convergence_gate`。

- 席位用 `Send` 并行，公共产物用 append-only reducer；
- 席位草稿/消息只存在于 `SeatTask.scratch`，不回写共享状态；
- 收敛门禁返回 `Command` 定向研究、再来一轮、judge、insufficient 或 escalation；
- 只有 `RevisionCommitter` 能改规范假设/因果图。
"""

from collections.abc import Mapping, Sequence
from typing import Annotated, Any, TypedDict, TypeVar

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, Send

from ..contracts import V4Contract
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
    Rebuttal,
    RevisionProposal,
)
from ..contracts.evidence import EvidenceItem, EvidenceLedger
from ..contracts.research import CausalEdge, Hypothesis, ResearchGap, RevisionEvent
from ..graph.state import canonical_digest, merge_by_id, sum_usage
from .agenda import build_agenda, merge_submission_batches
from .beliefs import belief_update_from_peer_brief, make_hold_update
from .committer import RevisionCommitter
from .convergence import NextAction, evaluate_convergence, validate_objection
from .judge import CouncilJudge
from .peer_briefs import build_peer_briefs
from .roles import LEAD_ROLE, run_role_node, select_roles

ModelT = TypeVar("ModelT", bound=V4Contract)


def _merge_models(
    left: Sequence[ModelT],
    right: Sequence[ModelT],
    *,
    model: type[ModelT],
    id_field: str,
) -> list[ModelT]:
    merged = merge_by_id(
        [item.model_dump() for item in left],
        [item.model_dump() for item in right],
        id_field,
    )
    return [model.model_validate(item) for item in merged.items]


def _merge_agenda_items(
    left: Sequence[AgendaItem], right: Sequence[AgendaItem]
) -> list[AgendaItem]:
    return _merge_models(left, right, model=AgendaItem, id_field="issue_id")


def _merge_objections(
    left: Sequence[Objection], right: Sequence[Objection]
) -> list[Objection]:
    return _merge_models(left, right, model=Objection, id_field="objection_id")


def _merge_rebuttals(
    left: Sequence[Rebuttal], right: Sequence[Rebuttal]
) -> list[Rebuttal]:
    return _merge_models(left, right, model=Rebuttal, id_field="rebuttal_id")


def _merge_peer_briefs(
    left: Sequence[PeerBrief], right: Sequence[PeerBrief]
) -> list[PeerBrief]:
    return _merge_models(left, right, model=PeerBrief, id_field="peer_brief_id")


def _merge_belief_updates(
    left: Sequence[BeliefUpdate], right: Sequence[BeliefUpdate]
) -> list[BeliefUpdate]:
    return _merge_models(left, right, model=BeliefUpdate, id_field="belief_update_id")


def _merge_influence_edges(
    left: Sequence[InfluenceEdge], right: Sequence[InfluenceEdge]
) -> list[InfluenceEdge]:
    return _merge_models(
        left, right, model=InfluenceEdge, id_field="influence_edge_id"
    )


def _merge_revision_proposals(
    left: Sequence[RevisionProposal], right: Sequence[RevisionProposal]
) -> list[RevisionProposal]:
    return _merge_models(left, right, model=RevisionProposal, id_field="proposal_id")


def _merge_revision_events(
    left: Sequence[RevisionEvent], right: Sequence[RevisionEvent]
) -> list[RevisionEvent]:
    return _merge_models(left, right, model=RevisionEvent, id_field="revision_id")


def _merge_round_ledgers(
    left: Sequence[DeliberationRoundLedger],
    right: Sequence[DeliberationRoundLedger],
) -> list[DeliberationRoundLedger]:
    kept: dict[str, DeliberationRoundLedger] = {}
    digests: dict[str, str] = {}
    for ledger in (*left, *right):
        key = f"{ledger.deliberation_round:03d}:{ledger.case_version:03d}"
        digest = canonical_digest(ledger.model_dump())
        if key not in kept or digest < digests[key]:
            kept[key] = ledger
            digests[key] = digest
    return [kept[key] for key in sorted(kept)]


def _concat_records(
    left: list[dict[str, Any]], right: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [*left, *right]


class SeatScratch(TypedDict, total=False):
    """席位私有草稿；只存在于并行任务输入，绝不写回共享状态。"""

    messages: list[str]
    private_notes: list[str]


class SeatTask(TypedDict, total=False):
    """单个席位子图的输入投影。"""

    role: str
    round_no: int
    case_version: int
    case_file: CaseFile
    evidence_ledger: EvidenceLedger
    hypotheses: list[Hypothesis]
    agenda_items: list[AgendaItem]
    submissions: list[PositionPaper]
    peer_briefs: list[PeerBrief]
    belief_updates: list[BeliefUpdate]
    scratch: SeatScratch


class DeliberationState(TypedDict, total=False):
    """议会图状态；规范图与裁决为单写通道，其余并行通道均带 reducer。"""

    case_file: CaseFile
    evidence_ledger: EvidenceLedger
    hypotheses: list[Hypothesis]
    hypothesis_graph: dict[str, Hypothesis]
    causal_graph: list[CausalEdge]
    research_gaps: list[ResearchGap]
    collaboration_decision: CollaborationDecision
    agenda_items: Annotated[list[AgendaItem], _merge_agenda_items]
    submissions: Annotated[list[PositionPaper], merge_submission_batches]
    objections: Annotated[list[Objection], _merge_objections]
    rebuttals: Annotated[list[Rebuttal], _merge_rebuttals]
    peer_briefs: Annotated[list[PeerBrief], _merge_peer_briefs]
    belief_updates: Annotated[list[BeliefUpdate], _merge_belief_updates]
    influence_edges: Annotated[list[InfluenceEdge], _merge_influence_edges]
    revision_proposals: Annotated[list[RevisionProposal], _merge_revision_proposals]
    revision_events: Annotated[list[RevisionEvent], _merge_revision_events]
    deliberation_round_ledgers: Annotated[
        list[DeliberationRoundLedger], _merge_round_ledgers
    ]
    council_decision: CouncilDecision
    active_roles: list[str]
    round_no: int
    case_version: int
    new_evidence_count: int
    affected_role_ids: list[str]
    affected_hypothesis_refs: list[str]
    pending_gap_ids: list[str]
    resolved_gap_ids: list[str]
    committed_proposal_ids: list[str]
    last_ranking: list[str]
    stall_count: int
    next_action: str
    stop_reason: str | None
    run_status: str
    usage: Annotated[list[dict[str, Any]], sum_usage]
    errors: Annotated[list[dict[str, Any]], _concat_records]
    conflicts: Annotated[list[dict[str, Any]], _concat_records]


def _state_get(state: Mapping[str, Any], key: str, default: Any) -> Any:
    return state.get(key, default)


def _case_file(state: Mapping[str, Any]) -> CaseFile:
    case_file = state.get("case_file")
    if not isinstance(case_file, CaseFile):
        raise ValueError("council graph requires a CaseFile in state")
    return case_file


def _round_from_id(identifier: str) -> int | None:
    for part in identifier.split("_"):
        if part.startswith("r") and part[1:].isdigit():
            return int(part[1:])
    return None


def _latest_by_role(
    submissions: Sequence[PositionPaper],
) -> dict[str, PositionPaper]:
    latest: dict[str, PositionPaper] = {}
    for paper in sorted(submissions, key=lambda item: item.position_id):
        latest[paper.analyst_role] = paper
    return latest


def _seat_task(state: Mapping[str, Any], role: str) -> SeatTask:
    return SeatTask(
        role=role,
        round_no=int(_state_get(state, "round_no", 0)),
        case_version=int(_state_get(state, "case_version", 1)),
        case_file=_state_get(state, "case_file", None),
        evidence_ledger=_state_get(state, "evidence_ledger", None),
        hypotheses=list(_state_get(state, "hypotheses", ())),
        agenda_items=list(_state_get(state, "agenda_items", ())),
        submissions=list(_state_get(state, "submissions", ())),
        peer_briefs=list(_state_get(state, "peer_briefs", ())),
        belief_updates=list(_state_get(state, "belief_updates", ())),
        scratch=SeatScratch(messages=[], private_notes=[]),
    )


def case_freeze(state: DeliberationState) -> dict[str, Any]:
    case_file = _case_file(state)
    hypotheses = list(state.get("hypotheses", ()))
    collaboration = state.get("collaboration_decision")
    mode = collaboration.mode if collaboration is not None else "ANALYST_COUNCIL"
    selected = list(collaboration.selected_roles) if collaboration is not None else []
    active_roles = selected or select_roles(mode)
    if not active_roles:
        active_roles = [LEAD_ROLE]
    return {
        "hypothesis_graph": {hypothesis.ref: hypothesis for hypothesis in hypotheses},
        "round_no": 0,
        "case_version": case_file.case_version,
        "active_roles": active_roles,
        "new_evidence_count": 0,
        "stall_count": 0,
        "last_ranking": [],
        "pending_gap_ids": [],
        "affected_role_ids": [],
        "affected_hypothesis_refs": [],
        "resolved_gap_ids": [],
        "committed_proposal_ids": [],
        "conflicts": [],
        "run_status": "deliberating",
        "stop_reason": None,
    }


def agenda_build(state: DeliberationState) -> dict[str, Any]:
    case_file = _case_file(state)
    items = build_agenda(
        case_file,
        list(state.get("hypotheses", ())),
        list(state.get("research_gaps", ())),
    )
    return {"agenda_items": items}


def r0_independent_positions(state: DeliberationState) -> dict[str, Any]:
    return {}


def dispatch_r0(state: DeliberationState) -> list[Send]:
    return [
        Send("seat_r0", _seat_task(state, role))
        for role in state.get("active_roles", ())
    ]


def seat_position_node(task: SeatTask) -> dict[str, Any]:
    role = task.get("role", "")
    if not role:
        return {"submissions": [], "usage": []}
    return run_role_node(role, task)


def fact_normalize(state: DeliberationState) -> dict[str, Any]:
    # 证据去重与时间校验由研究层完成；此处只固定规范排序，避免下游读取漂移。
    return {}


def peer_brief_reveal(state: DeliberationState) -> dict[str, Any]:
    case_file = _case_file(state)
    briefs = build_peer_briefs(
        case_file=case_file,
        round_no=int(state.get("round_no", 0)),
        active_roles=list(state.get("active_roles", ())),
        submissions=list(state.get("submissions", ())),
        agenda_items=list(state.get("agenda_items", ())),
        resolved_gap_ids=list(state.get("resolved_gap_ids", ())),
    )
    return {"peer_briefs": briefs}


def cross_exam(state: DeliberationState) -> dict[str, Any]:
    round_no = int(state.get("round_no", 0))
    if round_no != 1:
        return {}
    latest = _latest_by_role(list(state.get("submissions", ())))
    lead = latest.get(LEAD_ROLE)
    alternative = latest.get("alternative")
    if lead is None or alternative is None or lead.hypothesis_ref == alternative.hypothesis_ref:
        return {}
    case_version = int(state.get("case_version", 1))
    objection = Objection(
        objection_id=f"obj_r01_alt_v{case_version:02d}",
        issue_id=lead.issue_id,
        raised_by="alternative",
        target_type="position",
        target_id=lead.position_id,
        objection_type="chronology",
        severity="high",
        argument="盘面价格先于公开消息变动，主因时间线需要补证",
        required_evidence=[],
        kill_criterion="若盘前触发证据成立，则流动性主因解释成立",
        status="open",
    )
    validate_objection(objection)
    rebuttal = Rebuttal(
        rebuttal_id=f"reb_r01_lead_v{case_version:02d}",
        objection_id=objection.objection_id,
        answered_by=LEAD_ROLE,
        response="request_more_evidence",
        residual_uncertainty="盘前触发时点尚未确认",
    )
    return {"objections": [objection], "rebuttals": [rebuttal]}


def belief_update(state: DeliberationState) -> dict[str, Any]:
    return {}


def dispatch_belief(state: DeliberationState) -> list[Send]:
    return [
        Send("seat_belief", _seat_task(state, role))
        for role in state.get("active_roles", ())
    ]


def seat_belief_node(task: SeatTask) -> dict[str, Any]:
    role = task.get("role", "")
    round_no = int(task.get("round_no", 0))
    case_version = int(task.get("case_version", 1))
    submissions = list(task.get("submissions", ()))
    latest = _latest_by_role(submissions).get(role)
    if role == "" or latest is None:
        return {"belief_updates": [], "influence_edges": [], "usage": []}

    briefs = [
        brief
        for brief in task.get("peer_briefs", ())
        if brief.target_role == role and brief.deliberation_round == round_no
    ]
    brief = sorted(briefs, key=lambda item: item.peer_brief_id)[-1] if briefs else None

    if brief is None:
        result = make_hold_update(
            role=role,
            issue_id=latest.issue_id,
            case_version=case_version,
            round_no=round_no,
            prior_hypothesis_ref=latest.hypothesis_ref,
            prior_confidence=latest.confidence,
        )
    else:
        result = belief_update_from_peer_brief(
            brief,
            prior_hypothesis_ref=latest.hypothesis_ref,
            prior_confidence=latest.confidence,
            role=role,
            round_no=round_no,
            case_version=case_version,
            issue_id=latest.issue_id,
        )

    update = BeliefUpdate.model_validate(
        result.model_dump(exclude={"influence_edges"})
    )
    return {
        "belief_updates": [update],
        "influence_edges": result.influence_edges,
        "usage": [{"key": "steps", "value": 1}],
    }


def _ranking(state: Mapping[str, Any], round_no: int) -> list[str]:
    active_roles = list(_state_get(state, "active_roles", ()))
    beliefs = [
        belief
        for belief in _state_get(state, "belief_updates", ())
        if belief.deliberation_round == round_no
    ]
    by_role = {belief.analyst_role: belief for belief in beliefs}
    latest = _latest_by_role(list(_state_get(state, "submissions", ())))

    totals: dict[str, list[float]] = {}
    for role in active_roles:
        belief = by_role.get(role)
        if belief is not None and belief.posterior_hypothesis_refs:
            for ref, confidence in zip(
                belief.posterior_hypothesis_refs,
                belief.posterior_confidence,
                strict=False,
            ):
                totals.setdefault(ref, []).append(float(confidence))
            continue
        paper = latest.get(role)
        if paper is not None:
            totals.setdefault(paper.hypothesis_ref, []).append(paper.confidence)

    averaged = [
        (ref, sum(values) / len(values)) for ref, values in totals.items()
    ]
    return [ref for ref, _ in sorted(averaged, key=lambda item: (-item[1], item[0]))]


def convergence_gate_node(state: DeliberationState) -> Command[NextAction]:
    case_file = _case_file(state)
    round_no = int(state.get("round_no", 0))
    beliefs = [
        belief
        for belief in state.get("belief_updates", ())
        if belief.deliberation_round == round_no
    ]
    objections = [
        objection
        for objection in state.get("objections", ())
        if _round_from_id(objection.objection_id) == round_no
    ]
    open_high = sorted(
        objection.objection_id
        for objection in objections
        if objection.status == "open" and objection.severity == "high"
    )
    pending_gaps = sorted(
        {gap for belief in beliefs for gap in belief.requested_gap_ids}
    )
    affected_roles = sorted(
        {belief.analyst_role for belief in beliefs if belief.requested_gap_ids}
    )
    affected_refs = sorted(
        {belief.prior_hypothesis_ref for belief in beliefs if belief.requested_gap_ids}
    )
    max_delta = 0.0
    for belief in beliefs:
        posteriors = belief.posterior_confidence or [belief.prior_confidence]
        for posterior in posteriors:
            max_delta = max(max_delta, abs(posterior - belief.prior_confidence))

    ranking = _ranking(state, round_no)
    last_ranking = list(state.get("last_ranking", ()))
    ranking_changed = bool(last_ranking) and ranking != last_ranking

    decision = evaluate_convergence(
        profile=case_file.profile,
        round_no=round_no,
        new_evidence_count=int(state.get("new_evidence_count", 0)),
        open_high_objection_ids=open_high,
        pending_gap_ids=pending_gaps,
        ranking_changed=ranking_changed,
        topology_changed=False,
        max_belief_delta=max_delta,
    )
    next_action: NextAction = decision.next_action
    stop_reason = decision.stop_reason
    if next_action == "targeted_research" and (not pending_gaps or not affected_roles):
        next_action = "judge"
        stop_reason = "research_without_affected_seat"

    peer_brief_ids = sorted(
        brief.peer_brief_id
        for brief in state.get("peer_briefs", ())
        if brief.deliberation_round == round_no
    )
    belief_ids = sorted(belief.belief_update_id for belief in beliefs)
    belief_id_set = set(belief_ids)
    influence_ids = sorted(
        edge.influence_edge_id
        for edge in state.get("influence_edges", ())
        if edge.target_belief_update_id in belief_id_set
    )
    ledger = DeliberationRoundLedger(
        deliberation_round=round_no,
        case_version=int(state.get("case_version", case_file.case_version)),
        active_issue_ids=sorted(
            item.issue_id for item in state.get("agenda_items", ())
        ),
        peer_brief_ids=peer_brief_ids,
        belief_update_ids=belief_ids,
        influence_edge_ids=influence_ids,
        new_evidence_count=int(state.get("new_evidence_count", 0)),
        closed_high_objection_ids=[],
        max_belief_delta=max_delta,
        ranking_changed=ranking_changed,
        topology_changed=False,
        affected_role_ids=affected_roles if next_action == "targeted_research" else [],
        next_action=next_action,
        stop_reason=stop_reason,
    )

    update: dict[str, Any] = {
        "deliberation_round_ledgers": [ledger],
        "last_ranking": ranking,
        "next_action": next_action,
        "new_evidence_count": 0,
        "stall_count": (
            0 if decision.has_progress else int(state.get("stall_count", 0)) + 1
        ),
        "run_status": "deliberating",
    }
    if next_action == "targeted_research":
        update["pending_gap_ids"] = pending_gaps
        update["affected_role_ids"] = affected_roles
        update["affected_hypothesis_refs"] = affected_refs
    if stop_reason is not None:
        update["stop_reason"] = stop_reason

    return Command(goto=next_action, update=update)


def next_round(state: DeliberationState) -> dict[str, Any]:
    return {
        "round_no": int(state.get("round_no", 0)) + 1,
        "new_evidence_count": 0,
        "run_status": "deliberating",
    }


def targeted_research(state: DeliberationState) -> Command[str]:
    case_file = _case_file(state)
    pending_gaps = sorted(state.get("pending_gap_ids", ()))
    affected_roles = sorted(state.get("affected_role_ids", ()))
    affected_refs = sorted(state.get("affected_hypothesis_refs", ()))
    if not pending_gaps or not affected_roles or not affected_refs:
        return Command(
            goto="judge", update={"stop_reason": "research_without_target"}
        )

    ledger = state.get("evidence_ledger")
    if not isinstance(ledger, EvidenceLedger):
        raise ValueError("targeted_research requires an EvidenceLedger in state")

    new_version = int(state.get("case_version", case_file.case_version)) + 1
    evidence_id = f"ev_research_v{new_version}"
    evidence = EvidenceItem(
        evidence_id=evidence_id,
        source="targeted_research",
        source_type="timeline",
        event_time=case_file.as_of,
        published_at=case_file.as_of,
        fetched_at=case_file.as_of,
        as_of=case_file.as_of,
        evidence_kind="direct",
        supports_hypothesis_refs=affected_refs,
        data_quality="high",
        time_confidence="high",
        tool="targeted_research",
        response_digest=f"digest_research_v{new_version}",
        snapshot_id=case_file.snapshot_id,
    )
    new_ledger = EvidenceLedger(
        run_id=ledger.run_id,
        snapshot_id=ledger.snapshot_id,
        as_of=ledger.as_of,
        items=sorted([*ledger.items, evidence], key=lambda item: item.evidence_id),
        conflict_ids=sorted(ledger.conflict_ids),
    )

    first_ref = affected_refs[0]
    stem = first_ref.split(":", 1)[0]
    proposal = RevisionProposal(
        proposal_id=f"rp_v{new_version}_{stem}",
        issue_id=f"issue_{stem}",
        proposer=affected_roles[0],
        target_refs=affected_refs,
        action="add_evidence",
        reason=f"定向研究补充 Gap: {', '.join(pending_gaps)}",
        triggering_evidence_ids=[evidence_id],
        status="accepted",
    )

    return Command(
        goto="commit_revision",
        update={
            "evidence_ledger": new_ledger,
            "case_version": new_version,
            "round_no": int(state.get("round_no", 0)) + 1,
            "new_evidence_count": 0,
            "pending_gap_ids": [],
            "resolved_gap_ids": sorted(
                set(state.get("resolved_gap_ids", ())) | set(pending_gaps)
            ),
            "revision_proposals": [proposal],
            "run_status": "deliberating",
        },
    )


def commit_revision(state: DeliberationState) -> dict[str, Any]:
    committed = set(state.get("committed_proposal_ids", ()))
    pending = [
        proposal
        for proposal in sorted(
            state.get("revision_proposals", ()),
            key=lambda item: item.proposal_id,
        )
        if proposal.status == "accepted" and proposal.proposal_id not in committed
    ]
    if not pending:
        return {}

    case_file = _case_file(state)
    graph = dict(state.get("hypothesis_graph", {}))
    causal_graph = list(state.get("causal_graph", ()))
    events: list[RevisionEvent] = []
    committer = RevisionCommitter()

    for proposal in pending:
        result = committer.commit(
            proposal,
            {
                "case_file": case_file,
                "case_version": int(state.get("case_version", case_file.case_version)),
                "hypothesis_graph": graph,
                "causal_graph": causal_graph,
            },
        )
        graph = result.hypothesis_graph
        causal_graph = result.causal_graph
        events.extend(result.revision_events)

    return {
        "hypothesis_graph": graph,
        "causal_graph": causal_graph,
        "revision_events": events,
        "committed_proposal_ids": sorted(
            committed | {proposal.proposal_id for proposal in pending}
        ),
    }


def rerun_affected_positions(state: DeliberationState) -> dict[str, Any]:
    return {}


def dispatch_rerun(state: DeliberationState) -> list[Send]:
    return [
        Send("seat_rerun", _seat_task(state, role))
        for role in state.get("affected_role_ids", ())
    ]


def judge_node(state: DeliberationState) -> dict[str, Any]:
    case_file = _case_file(state)
    ledger = state.get("evidence_ledger")
    if not isinstance(ledger, EvidenceLedger):
        raise ValueError("judge requires an EvidenceLedger in state")
    judge = CouncilJudge(
        frozen_evidence_ids={item.evidence_id for item in ledger.items},
        rubric_version=case_file.decision_rubric_version,
    )
    decision = judge.judge(
        case_file=case_file,
        agenda_items=list(state.get("agenda_items", ())),
        positions=list(state.get("submissions", ())),
        objections=list(state.get("objections", ())),
        belief_updates=list(state.get("belief_updates", ())),
        rebuttals=list(state.get("rebuttals", ())),
    )
    return {
        "council_decision": decision,
        "run_status": "decided",
        "stop_reason": state.get("stop_reason") or "judged",
    }


def insufficient_node(state: DeliberationState) -> dict[str, Any]:
    return {
        "run_status": "insufficient",
        "stop_reason": state.get("stop_reason") or "insufficient",
    }


def escalation_node(state: DeliberationState) -> dict[str, Any]:
    return {
        "run_status": "escalated",
        "stop_reason": state.get("stop_reason") or "escalation",
    }


def build_council_graph() -> CompiledStateGraph[
    DeliberationState, None, DeliberationState, DeliberationState
]:
    """组装并编译案卷制议会图。"""
    builder: StateGraph[
        DeliberationState, None, DeliberationState, DeliberationState
    ] = StateGraph(DeliberationState)

    builder.add_node("case_freeze", case_freeze)
    builder.add_node("agenda_build", agenda_build)
    builder.add_node("r0_independent_positions", r0_independent_positions)
    # TypedDict 作为 `type[...]` 参数是 mypy 已知限制；运行期 LangGraph 正常。
    builder.add_node(
        "seat_r0",
        seat_position_node,  # type: ignore[arg-type]
        input_schema=SeatTask,
    )
    builder.add_node("fact_normalize", fact_normalize)
    builder.add_node("peer_brief_reveal", peer_brief_reveal)
    builder.add_node("cross_exam", cross_exam)
    builder.add_node("belief_update", belief_update)
    builder.add_node(
        "seat_belief",
        seat_belief_node,  # type: ignore[arg-type]
        input_schema=SeatTask,
    )
    builder.add_node("convergence_gate", convergence_gate_node)
    builder.add_node("next_round", next_round)
    builder.add_node("targeted_research", targeted_research)
    builder.add_node("commit_revision", commit_revision)
    builder.add_node("rerun_affected_positions", rerun_affected_positions)
    builder.add_node(
        "seat_rerun",
        seat_position_node,  # type: ignore[arg-type]
        input_schema=SeatTask,
    )
    builder.add_node("judge", judge_node)
    builder.add_node("insufficient", insufficient_node)
    builder.add_node("escalation", escalation_node)

    builder.add_edge(START, "case_freeze")
    builder.add_edge("case_freeze", "agenda_build")
    builder.add_edge("agenda_build", "r0_independent_positions")
    builder.add_conditional_edges(
        "r0_independent_positions", dispatch_r0, ["seat_r0"]
    )
    builder.add_edge("seat_r0", "fact_normalize")
    builder.add_edge("fact_normalize", "peer_brief_reveal")
    builder.add_edge("peer_brief_reveal", "cross_exam")
    builder.add_edge("cross_exam", "belief_update")
    builder.add_conditional_edges("belief_update", dispatch_belief, ["seat_belief"])
    builder.add_edge("seat_belief", "convergence_gate")
    builder.add_edge("next_round", "peer_brief_reveal")
    builder.add_edge("commit_revision", "rerun_affected_positions")
    builder.add_conditional_edges(
        "rerun_affected_positions", dispatch_rerun, ["seat_rerun"]
    )
    builder.add_edge("seat_rerun", "fact_normalize")
    builder.add_edge("judge", END)
    builder.add_edge("insufficient", END)
    builder.add_edge("escalation", END)

    return builder.compile()
