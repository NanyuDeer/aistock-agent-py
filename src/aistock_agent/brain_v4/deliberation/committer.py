"""唯一写者：把 accepted `RevisionProposal` 写入规范假设图与因果图。

并行席位只能产出 `PositionPaper / Objection / RevisionProposal` 等输入；
只有本模块的 `RevisionCommitter.commit` 能把提案转成 `RevisionEvent` 并
更新 `hypothesis_graph` / `causal_graph`。提案状态不是 accepted 时一律拒绝，
避免“提案即生效”。
"""

from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.deliberation import CaseFile, RevisionProposal
from ..contracts.research import (
    CausalEdge,
    Hypothesis,
    HypothesisStatus,
    RevisionEvent,
)


class CommitResult(V4Contract):
    """一次提交后的规范图与回改事件。"""

    hypothesis_graph: dict[str, Hypothesis] = Field(default_factory=dict)
    causal_graph: list[CausalEdge] = Field(default_factory=list)
    revision_events: list[RevisionEvent] = Field(default_factory=list)


def _stem(ref: str) -> str:
    return ref.split(":", 1)[0]


def _case_file(state: Mapping[str, Any]) -> CaseFile:
    case_file = state.get("case_file")
    if not isinstance(case_file, CaseFile):
        raise ValueError("RevisionCommitter.commit requires a CaseFile in state")
    return case_file


def _hypothesis_graph(state: Mapping[str, Any]) -> dict[str, Hypothesis]:
    graph = state.get("hypothesis_graph")
    if isinstance(graph, Mapping):
        return {str(ref): hypothesis for ref, hypothesis in graph.items()}
    hypotheses = state.get("hypotheses", ())
    return {hypothesis.ref: hypothesis for hypothesis in hypotheses}


def _new_hypothesis_version(
    hypothesis: Hypothesis, status: HypothesisStatus
) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=hypothesis.hypothesis_id,
        version=hypothesis.version + 1,
        statement=hypothesis.statement,
        status=status,
        prior_confidence=hypothesis.prior_confidence,
        assumptions=hypothesis.assumptions,
        disconfirmers=hypothesis.disconfirmers,
        required_evidence_types=hypothesis.required_evidence_types,
        parent_ids=[*hypothesis.parent_ids, hypothesis.ref],
        scope=hypothesis.scope,
    )


def _apply_hypothesis_action(
    graph: dict[str, Hypothesis], proposal: RevisionProposal
) -> dict[str, Hypothesis]:
    updated = dict(graph)
    targets = {
        ref: hypothesis
        for ref, hypothesis in graph.items()
        if ref in proposal.target_refs or _stem(ref) in {_stem(t) for t in proposal.target_refs}
    }
    if not targets:
        raise ValueError(
            f"proposal {proposal.proposal_id} targets unknown hypothesis refs "
            f"{proposal.target_refs}"
        )
    status: HypothesisStatus = (
        "rejected" if proposal.action == "reject" else "weakened"
    )
    for hypothesis in sorted(targets.values(), key=lambda item: item.ref):
        revised = _new_hypothesis_version(hypothesis, status)
        updated[revised.ref] = revised
    return {ref: updated[ref] for ref in sorted(updated)}


def _apply_add_evidence(
    graph: Sequence[CausalEdge], proposal: RevisionProposal
) -> list[CausalEdge]:
    target_stems = {_stem(ref) for ref in proposal.target_refs}
    evidence_ids = sorted(set(proposal.triggering_evidence_ids))
    updated: list[CausalEdge] = []
    for edge in graph:
        if _stem(edge.from_node) in target_stems or _stem(edge.to_node) in target_stems:
            merged = sorted(set(edge.supporting_evidence_ids) | set(evidence_ids))
            updated.append(edge.model_copy(update={"supporting_evidence_ids": merged}))
        else:
            updated.append(edge)
    return sorted(updated, key=lambda item: item.edge_id)


class RevisionCommitter:
    """唯一允许修改规范图的组件。"""

    def commit(
        self, proposal: RevisionProposal, state: Mapping[str, Any]
    ) -> CommitResult:
        if proposal.status != "accepted":
            raise ValueError(
                f"proposal {proposal.proposal_id} status={proposal.status!r}; "
                "only accepted proposals can be committed"
            )

        case_file = _case_file(state)
        graph = _hypothesis_graph(state)
        causal_graph = list(state.get("causal_graph", ()))
        case_version = int(state.get("case_version", case_file.case_version))

        if proposal.action == "add_evidence":
            causal_graph = _apply_add_evidence(causal_graph, proposal)
        elif proposal.action in {"reweight", "revise", "reject"}:
            graph = _apply_hypothesis_action(graph, proposal)
        else:
            raise ValueError(
                f"proposal {proposal.proposal_id} action={proposal.action!r} "
                "requires an explicit new-hypothesis payload and is not supported"
            )

        event = RevisionEvent(
            revision_id=f"rev_{proposal.proposal_id}",
            run_id=case_file.run_id,
            action=proposal.action,
            target_refs=list(proposal.target_refs),
            triggering_evidence_ids=sorted(proposal.triggering_evidence_ids),
            impacted_hypothesis_refs=list(proposal.target_refs),
            impacted_claim_ids=sorted(proposal.expected_impact_refs),
            reason=proposal.reason,
            case_version=case_version,
            created_at=case_file.as_of,
        )
        return CommitResult(
            hypothesis_graph=graph,
            causal_graph=causal_graph,
            revision_events=[event],
        )
