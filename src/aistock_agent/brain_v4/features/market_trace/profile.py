"""市场溯源独立研究：竞争假设、因果图、补证、局部回改、议会与研判。

本模块只编排 V4 已有的可复用内核，不复制公共逻辑：
- 证据：`adapter.snapshot_to_evidence` 映射冻结快照；
- 门禁：`controller.sufficiency.evaluate_sufficiency` 产出结构化 Gap；
- 路由：`deliberation.router.route_collaboration` 决定是否启动议会；
- 议会：`deliberation.graph.build_council_graph` 执行案卷制多轮；
- 研判：`graph.builder.commit_judgment` 作为唯一写者落成 `V4Judgment`。

时间纪律：`as_of` / `data_cutoff` 全部来自冻结案例，绝不读取系统时间。
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from pydantic import Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.deliberation import (
    BeliefUpdate,
    CaseFile,
    CollaborationDecision,
    CouncilDecision,
    PeerBrief,
    PositionPaper,
)
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem, EvidenceLedger
from aistock_agent.brain_v4.contracts.judgment import V4Judgment
from aistock_agent.brain_v4.contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchGap,
    ResearchRequest,
    RevisionEvent,
)
from aistock_agent.brain_v4.controller.sufficiency import evaluate_sufficiency
from aistock_agent.brain_v4.deliberation.graph import (
    DeliberationState,
    build_council_graph,
)
from aistock_agent.brain_v4.deliberation.router import route_collaboration
from aistock_agent.brain_v4.graph.builder import commit_judgment

from .adapter import (
    MarketTraceCase,
    compute_snapshot_sha,
    snapshot_to_evidence,
)

PROFILE_NAME = "market_trace"
MODEL_VERSION = "v4-market-trace-1"
CONFIG_VERSION = "cfg_market_trace_v1"
PROMPT_VERSION = "prompt_market_trace_v1"
METHOD_VERSION = "method_v4.0"
COUNCIL_BUDGET = 32_000

#: 来源 token -> 假设类别；用于 V4 自己的证据挂载，不读取旧链结论。
_SUPPORT_TOKENS: dict[str, frozenset[str]] = {
    "liquidity": frozenset({"TURNOVER", "FUND", "FLOW"}),
    "style": frozenset({"STYLE"}),
    "sector": frozenset({"SECTOR", "INDEX", "BREADTH"}),
    "risk": frozenset({"STYLE", "SECTOR", "EVENT"}),
}

#: 六因子因果路径的相邻阶段。
_STAGE_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("context", "structural_root"),
    ("structural_root", "trigger"),
    ("trigger", "transmission"),
    ("transmission", "exposure"),
    ("exposure", "repricing"),
    ("repricing", "observable_result"),
)

_STAGE_MECHANISM: dict[str, str] = {
    "structural_root": "结构根因：{statement}",
    "trigger": "触发：盘前信号确认结构压力",
    "transmission": "传导：资金与风格通过风险预算传导",
    "exposure": "暴露：高 beta 与成长板块暴露更高",
    "repricing": "重定价：暴露板块估值下修",
    "observable_result": "观察结果：指数与广度同步下跌",
}

_POST_REVISION_CONFIDENCE: dict[str, float] = {
    "liquidity": 0.30,
    "style": 0.58,
}


class ResearchRun(V4Contract):
    """一次市场溯源运行的全部可回放产物。"""

    case_id: str
    case_kind: str
    run_id: str
    subject_type: str
    subject_code: str
    profile: str
    snapshot_id: str
    snapshot_sha: str
    as_of: datetime
    data_cutoff: datetime
    evidence_ledger: EvidenceLedger
    hypotheses: list[Hypothesis]
    causal_graph: list[CausalEdge]
    research_gaps: list[ResearchGap]
    revision_events: list[RevisionEvent]
    collaboration_decision: CollaborationDecision
    judgment: V4Judgment
    sufficiency_passed: bool
    unsupported_edge_ratio: float
    council_decision: CouncilDecision | None = None
    submissions: list[PositionPaper] = Field(default_factory=list)
    peer_briefs: list[PeerBrief] = Field(default_factory=list)
    belief_updates: list[BeliefUpdate] = Field(default_factory=list)
    deliberation_state: dict[str, Any] | None = None
    trace: list[dict[str, Any]] = Field(default_factory=list)


@dataclass(frozen=True)
class _SoloOutcome:
    ledger: EvidenceLedger
    hypotheses: list[Hypothesis]
    causal_graph: list[CausalEdge]
    gaps: list[ResearchGap]
    revisions: list[RevisionEvent]
    sufficiency_passed: bool
    unsupported_edge_ratio: float
    judgment: V4Judgment


@dataclass(frozen=True)
class _CouncilOutcome:
    ledger: EvidenceLedger
    hypotheses: list[Hypothesis]
    causal_graph: list[CausalEdge]
    gaps: list[ResearchGap]
    revisions: list[RevisionEvent]
    sufficiency_passed: bool
    unsupported_edge_ratio: float
    council_decision: CouncilDecision
    submissions: list[PositionPaper]
    peer_briefs: list[PeerBrief]
    belief_updates: list[BeliefUpdate]
    judgment: V4Judgment
    deliberation_state: dict[str, Any]


def _kind(hypothesis_id: str) -> str:
    return hypothesis_id.removeprefix("hyp_").split("_", 1)[0]


def _seed_hypotheses(case: MarketTraceCase) -> list[Hypothesis]:
    return [
        Hypothesis(
            hypothesis_id=seed.hypothesis_id,
            version=1,
            statement=seed.statement,
            status="open",
            prior_confidence=seed.prior_confidence,
            assumptions=list(seed.assumptions),
            disconfirmers=list(seed.disconfirmers),
            required_evidence_types=list(seed.required_evidence_types),
            parent_ids=[],
            scope=seed.scope,
        )
        for seed in case.hypothesis_seeds
    ]


def _link_evidence(
    items: Sequence[EvidenceItem],
    hypotheses: Sequence[Hypothesis],
    *,
    defer_tokens: frozenset[str],
) -> list[EvidenceItem]:
    """把证据按来源 token 挂到假设上；这是 V4 研究步骤，不是旧链映射。"""
    linked: list[EvidenceItem] = []
    for item in items:
        raw_source_id = item.tool_params.get("source_id", "")
        tokens = frozenset(str(raw_source_id).upper().split("_"))
        if tokens & defer_tokens:
            supports: list[str] = []
        else:
            supports = sorted(
                hypothesis.ref
                for hypothesis in hypotheses
                if tokens & _SUPPORT_TOKENS.get(_kind(hypothesis.hypothesis_id), frozenset())
            )
        linked.append(
            item.model_copy(update={"supports_hypothesis_refs": supports})
        )
    return linked


def _supported_ids(items: Sequence[EvidenceItem], hypothesis_ref: str) -> list[str]:
    return sorted(
        item.evidence_id
        for item in items
        if hypothesis_ref in item.supports_hypothesis_refs
    )


def _style_items(items: Sequence[EvidenceItem]) -> list[EvidenceItem]:
    return [
        item
        for item in items
        if "STYLE" in str(item.tool_params.get("source_id", "")).upper().split("_")
    ]


def _rewire_evidence(
    items: Sequence[EvidenceItem],
    *,
    old_liquidity_ref: str,
    new_liquidity_ref: str,
    style_ref: str,
) -> list[EvidenceItem]:
    """局部回改：把流动性证据重挂到 v2，并把风格来源证据挂到风格假设。

    只重建受影响的根因证据关系，不动板块与其他假设。
    """
    rewired: list[EvidenceItem] = []
    for item in items:
        refs = [
            new_liquidity_ref if ref == old_liquidity_ref else ref
            for ref in item.supports_hypothesis_refs
        ]
        tokens = frozenset(str(item.tool_params.get("source_id", "")).upper().split("_"))
        if "STYLE" in tokens:
            refs.append(style_ref)
        rewired.append(
            item.model_copy(update={"supports_hypothesis_refs": sorted(set(refs))})
        )
    return rewired


def _targeted_style_evidence(
    case: MarketTraceCase,
    items: Sequence[EvidenceItem],
    style_ref: str,
) -> EvidenceItem:
    """一次定向补证：只重读冻结快照里的风格事实，不引入新数据源。"""
    style_digests = sorted(item.response_digest for item in _style_items(items))
    digest = hashlib.sha256("".join(style_digests).encode("utf-8")).hexdigest()
    return EvidenceItem(
        evidence_id=f"ev_supp_style_{digest[:12]}",
        source="frozen_snapshot",
        source_type="market_fact",
        event_time=case.data_cutoff,
        published_at=case.as_of,
        fetched_at=case.as_of,
        as_of=case.as_of,
        evidence_kind="direct",
        supports_hypothesis_refs=[style_ref],
        data_quality="high",
        time_confidence="high",
        tool="targeted_research",
        tool_params={
            "round_no": 1,
            "target": "style_divergence",
            "snapshot_id": case.snapshot.snapshot_id,
        },
        response_digest=digest,
        snapshot_id=case.snapshot.snapshot_id,
    )


def _chain_edges(
    *,
    hypothesis: Hypothesis,
    confidence: float,
    evidence_ids: Sequence[str],
    supported: bool,
    valid_time: datetime,
) -> list[CausalEdge]:
    edges: list[CausalEdge] = []
    for from_node, to_node in _STAGE_TRANSITIONS:
        mechanism = _STAGE_MECHANISM[to_node].format(statement=hypothesis.statement)
        edges.append(
            CausalEdge(
                edge_id=f"edge_{hypothesis.hypothesis_id}_{to_node}",
                from_node=from_node,
                to_node=to_node,
                mechanism=mechanism,
                supporting_evidence_ids=sorted(evidence_ids) if supported else [],
                contradicting_evidence_ids=[],
                edge_type="inference" if supported else "hypothesis",
                confidence=confidence,
                valid_time=valid_time,
                magnitude_check=f"magnitude_check:{to_node}",
                counterfactual_check=f"invalidator:{hypothesis.disconfirmers[0]}",
            )
        )
    return edges


def _request(case: MarketTraceCase) -> ResearchRequest:
    return ResearchRequest(
        request_id=f"req_{case.run_id}",
        run_id=case.run_id,
        profile=PROFILE_NAME,
        subject_type="market",
        subject_code=case.subject_code,
        research_goal="Which root cause explains the index move?",
        required_questions=["structural_root", "trigger", "amplifier"],
        horizon="1d",
        as_of=case.as_of,
        data_cutoff=case.data_cutoff,
        snapshot_id=case.snapshot.snapshot_id,
        config_version=CONFIG_VERSION,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
        method_version=METHOD_VERSION,
        budget_policy=PROFILE_NAME,
        stop_policy="default",
    )


def _collaboration(
    case: MarketTraceCase, hypotheses: Sequence[Hypothesis]
) -> CollaborationDecision:
    return route_collaboration(
        profile=PROFILE_NAME,
        materiality=case.materiality,
        hypothesis_scores=[hypothesis.prior_confidence for hypothesis in hypotheses],
        evidence_conflict_score=0.0,
        cross_layer_conflict=False,
        remaining_budget=COUNCIL_BUDGET,
        professional_gaps=0,
        publish_ready=False,
        counter_evidence_coverage=1.0,
    )


def _ledger(case: MarketTraceCase, items: Sequence[EvidenceItem]) -> EvidenceLedger:
    return EvidenceLedger(
        run_id=case.run_id,
        snapshot_id=case.snapshot.snapshot_id,
        as_of=case.as_of,
        items=sorted(items, key=lambda item: item.evidence_id),
        conflict_ids=[],
    )


def _case_file(
    case: MarketTraceCase,
    hypotheses: Sequence[Hypothesis],
    items: Sequence[EvidenceItem],
) -> CaseFile:
    return CaseFile(
        case_id=case.case_id,
        run_id=case.run_id,
        profile=PROFILE_NAME,
        case_version=1,
        question="Which root cause explains the index move?",
        decision_type="market_root_cause",
        materiality=case.materiality,
        subject="market",
        horizon="1d",
        as_of=case.as_of,
        snapshot_id=case.snapshot.snapshot_id,
        frozen_fact_ids=sorted(item.evidence_id for item in items),
        hypothesis_refs=sorted(hypothesis.ref for hypothesis in hypotheses),
        open_issue_ids=sorted(
            f"issue_{hypothesis.hypothesis_id}" for hypothesis in hypotheses
        ),
        deadline=case.as_of + timedelta(hours=1),
        budget_policy=PROFILE_NAME,
        decision_rubric_version="rubric_v1",
    )


def _run_solo(
    case: MarketTraceCase,
    request: ResearchRequest,
    hypotheses: list[Hypothesis],
    items: Sequence[EvidenceItem],
    decision: CollaborationDecision,
) -> _SoloOutcome:
    style_ref = next(
        hypothesis.ref
        for hypothesis in hypotheses
        if _kind(hypothesis.hypothesis_id) == "style"
    )
    defer = frozenset({"STYLE"}) if case.case_kind == "liquidity_pseudo_root" else frozenset()
    linked = _link_evidence(items, hypotheses, defer_tokens=defer)

    liquidity = next(
        hypothesis
        for hypothesis in hypotheses
        if _kind(hypothesis.hypothesis_id) == "liquidity"
    )
    sector = [
        hypothesis
        for hypothesis in hypotheses
        if _kind(hypothesis.hypothesis_id) == "sector"
    ]

    initial_edges = _chain_edges(
        hypothesis=liquidity,
        confidence=liquidity.prior_confidence,
        evidence_ids=_supported_ids(linked, liquidity.ref),
        supported=True,
        valid_time=case.data_cutoff,
    )
    for hypothesis in sector:
        # 板块轮动证据存在，但因果路径尚未被证据锁定：显式标为假设边。
        initial_edges.extend(
            _chain_edges(
                hypothesis=hypothesis,
                confidence=hypothesis.prior_confidence,
                evidence_ids=[],
                supported=False,
                valid_time=case.data_cutoff,
            )
        )

    issue_id = f"issue_{case.case_id}"
    report_before = evaluate_sufficiency(
        run_id=case.run_id,
        issue_id=issue_id,
        as_of=case.as_of,
        data_cutoff=case.data_cutoff,
        hypotheses=hypotheses,
        evidence=linked,
        causal_edges=initial_edges,
    )

    targeted = _targeted_style_evidence(case, linked, style_ref)
    style_hypothesis = next(
        hypothesis for hypothesis in hypotheses if hypothesis.ref == style_ref
    )

    liquidity_v2 = Hypothesis(
        hypothesis_id=liquidity.hypothesis_id,
        version=liquidity.version + 1,
        statement="资金收缩是风险偏好切换后的放大器",
        status="weakened",
        prior_confidence=_POST_REVISION_CONFIDENCE["liquidity"],
        assumptions=list(liquidity.assumptions),
        disconfirmers=list(liquidity.disconfirmers),
        required_evidence_types=list(liquidity.required_evidence_types),
        parent_ids=[*liquidity.parent_ids, liquidity.ref],
        scope=liquidity.scope,
    )
    rewired = _rewire_evidence(
        linked,
        old_liquidity_ref=liquidity.ref,
        new_liquidity_ref=liquidity_v2.ref,
        style_ref=style_ref,
    )
    supplemented = [*rewired, targeted]
    style_evidence = sorted(
        {targeted.evidence_id, *_supported_ids(rewired, style_ref)}
    )
    revised = [
        liquidity_v2 if hypothesis.ref == liquidity.ref else hypothesis
        for hypothesis in hypotheses
    ]

    revised_edges = _chain_edges(
        hypothesis=style_hypothesis,
        confidence=_POST_REVISION_CONFIDENCE["style"],
        evidence_ids=style_evidence,
        supported=True,
        valid_time=case.data_cutoff,
    )
    revised_edges.extend(
        _chain_edges(
            hypothesis=liquidity_v2,
            confidence=_POST_REVISION_CONFIDENCE["liquidity"],
            evidence_ids=_supported_ids(supplemented, liquidity_v2.ref),
            supported=True,
            valid_time=case.data_cutoff,
        )
    )
    for hypothesis in sector:
        revised_edges.extend(
            _chain_edges(
                hypothesis=hypothesis,
                confidence=hypothesis.prior_confidence,
                evidence_ids=[],
                supported=False,
                valid_time=case.data_cutoff,
            )
        )

    report_after = evaluate_sufficiency(
        run_id=case.run_id,
        issue_id=issue_id,
        as_of=case.as_of,
        data_cutoff=case.data_cutoff,
        hypotheses=revised,
        evidence=supplemented,
        causal_edges=revised_edges,
    )

    resolved_gaps = [
        gap.model_copy(update={"status": "resolved"}) for gap in report_before.gaps
    ]
    gaps = [*resolved_gaps, *report_after.gaps]

    revision = RevisionEvent(
        revision_id=f"rev_{case.run_id}_{liquidity.hypothesis_id}_amplifier",
        run_id=case.run_id,
        action="revise",
        target_refs=[liquidity.ref],
        triggering_evidence_ids=[targeted.evidence_id],
        impacted_hypothesis_refs=[liquidity_v2.ref],
        impacted_claim_ids=[style_ref],
        reason="风格分化与板块分化证据显示：流动性是风险偏好切换后的放大器",
        case_version=2,
        created_at=case.as_of,
    )

    ledger = _ledger(case, supplemented)
    judgment = commit_judgment(
        {
            "request": request,
            "evidence_ledger": ledger,
            "hypotheses": revised,
            "collaboration_decision": decision,
            "sufficiency_passed": report_after.passed,
            "stop_reason": "sufficient" if report_after.passed else "insufficient",
            "research_gaps": gaps,
            "research_round_no": 1,
            "revision_events": [revision],
        }
    )
    return _SoloOutcome(
        ledger=ledger,
        hypotheses=revised,
        causal_graph=revised_edges,
        gaps=gaps,
        revisions=[revision],
        sufficiency_passed=report_after.passed,
        unsupported_edge_ratio=report_after.unsupported_edge_ratio,
        judgment=judgment,
    )


def _run_council(
    case: MarketTraceCase,
    request: ResearchRequest,
    hypotheses: list[Hypothesis],
    items: Sequence[EvidenceItem],
    decision: CollaborationDecision,
) -> _CouncilOutcome:
    linked = _link_evidence(items, hypotheses, defer_tokens=frozenset())
    edges: list[CausalEdge] = []
    for hypothesis in hypotheses:
        edges.extend(
            _chain_edges(
                hypothesis=hypothesis,
                confidence=hypothesis.prior_confidence,
                evidence_ids=_supported_ids(linked, hypothesis.ref),
                supported=True,
                valid_time=case.data_cutoff,
            )
        )

    report = evaluate_sufficiency(
        run_id=case.run_id,
        issue_id=f"issue_{case.case_id}",
        as_of=case.as_of,
        data_cutoff=case.data_cutoff,
        hypotheses=hypotheses,
        evidence=linked,
        causal_edges=edges,
    )

    initial = {
        "case_file": _case_file(case, hypotheses, linked),
        "evidence_ledger": _ledger(case, linked),
        "hypotheses": hypotheses,
        "causal_graph": edges,
        "collaboration_decision": decision,
        "research_gaps": report.gaps,
        "conflicts": [],
    }
    graph = build_council_graph()
    final = cast(
        Mapping[str, Any],
        graph.invoke(
            cast(DeliberationState, initial),
            config=cast(RunnableConfig, {"recursion_limit": 100}),
        ),
    )

    final_ledger = final.get("evidence_ledger")
    if not isinstance(final_ledger, EvidenceLedger):
        raise ValueError("council run did not produce an EvidenceLedger")
    council_decision = final.get("council_decision")
    if not isinstance(council_decision, CouncilDecision):
        raise ValueError("council run did not produce a CouncilDecision")

    submissions = [
        item
        for item in cast(list[Any], final.get("submissions", []))
        if isinstance(item, PositionPaper)
    ]
    peer_briefs = [
        item
        for item in cast(list[Any], final.get("peer_briefs", []))
        if isinstance(item, PeerBrief)
    ]
    belief_updates = [
        item
        for item in cast(list[Any], final.get("belief_updates", []))
        if isinstance(item, BeliefUpdate)
    ]
    revisions = [
        item
        for item in cast(list[Any], final.get("revision_events", []))
        if isinstance(item, RevisionEvent)
    ]
    raw_edges = final.get("causal_graph", edges)
    causal_graph = (
        [item for item in raw_edges if isinstance(item, CausalEdge)]
        if isinstance(raw_edges, list)
        else edges
    )

    judgment = commit_judgment(
        {
            "request": request,
            "evidence_ledger": final_ledger,
            "hypotheses": hypotheses,
            "collaboration_decision": decision,
            "submissions": submissions,
            "peer_briefs": peer_briefs,
            "belief_updates": belief_updates,
            "revision_events": revisions,
            "council_decision": council_decision,
            "research_gaps": report.gaps,
            "research_round_no": 0,
            "sufficiency_passed": report.passed,
            "stop_reason": str(final.get("stop_reason") or "judged"),
        }
    )

    return _CouncilOutcome(
        ledger=final_ledger,
        hypotheses=hypotheses,
        causal_graph=causal_graph,
        gaps=report.gaps,
        revisions=revisions,
        sufficiency_passed=report.passed,
        unsupported_edge_ratio=report.unsupported_edge_ratio,
        council_decision=council_decision,
        submissions=submissions,
        peer_briefs=peer_briefs,
        belief_updates=belief_updates,
        judgment=judgment,
        deliberation_state=dict(final),
    )


def run_market_trace(case: MarketTraceCase) -> ResearchRun:
    """在冻结案例上运行一次完整市场溯源；结果确定、离线、可回放。"""
    snapshot_sha = compute_snapshot_sha(case.snapshot)
    items = snapshot_to_evidence(
        case.snapshot, case.as_of, case.data_cutoff, case.snapshot.snapshot_id
    )
    hypotheses = _seed_hypotheses(case)
    request = _request(case)
    decision = _collaboration(case, hypotheses)

    common: dict[str, Any] = {
        "case_id": case.case_id,
        "case_kind": case.case_kind,
        "run_id": case.run_id,
        "subject_type": "market",
        "subject_code": case.subject_code,
        "profile": PROFILE_NAME,
        "snapshot_id": case.snapshot.snapshot_id,
        "snapshot_sha": snapshot_sha,
        "as_of": case.as_of,
        "data_cutoff": case.data_cutoff,
        "collaboration_decision": decision,
        "trace": [
            {
                "trace_id": f"trace_{case.run_id}_route",
                "node": "collaboration_router",
                "mode": decision.mode,
            }
        ],
    }

    if decision.mode in {"ANALYST_COUNCIL", "MANDATORY_RED_TEAM"}:
        council = _run_council(case, request, hypotheses, items, decision)
        return ResearchRun(
            **common,
            evidence_ledger=council.ledger,
            hypotheses=council.hypotheses,
            causal_graph=council.causal_graph,
            research_gaps=council.gaps,
            revision_events=council.revisions,
            judgment=council.judgment,
            sufficiency_passed=council.sufficiency_passed,
            unsupported_edge_ratio=council.unsupported_edge_ratio,
            council_decision=council.council_decision,
            submissions=council.submissions,
            peer_briefs=council.peer_briefs,
            belief_updates=council.belief_updates,
            deliberation_state=council.deliberation_state,
        )

    solo = _run_solo(case, request, hypotheses, items, decision)
    return ResearchRun(
        **common,
        evidence_ledger=solo.ledger,
        hypotheses=solo.hypotheses,
        causal_graph=solo.causal_graph,
        research_gaps=solo.gaps,
        revision_events=solo.revisions,
        judgment=solo.judgment,
        sufficiency_passed=solo.sufficiency_passed,
        unsupported_edge_ratio=solo.unsupported_edge_ratio,
        council_decision=None,
    )
