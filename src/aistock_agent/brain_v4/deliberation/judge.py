"""议会裁判：只提交 `CouncilDecision`，不新增证据、不落库。

- 引用不在冻结 `EvidenceLedger` 内的证据必须拒绝；
- 采用逐项量表而非简单多数投票；
- 必须保留少数意见与未解异议，不能为了形成一致结论而抹平冲突。
"""

from collections.abc import Collection, Sequence
from typing import Literal

from ..contracts.deliberation import (
    AgendaItem,
    BeliefUpdate,
    CaseFile,
    CouncilDecision,
    Objection,
    PositionPaper,
    Rebuttal,
)

DecisionKind = Literal["accept", "revise", "insufficient", "escalate"]


class EvidenceNotFrozenError(ValueError):
    """裁决引用了冻结证据账本之外的证据。"""


def _latest_by_role(positions: Sequence[PositionPaper]) -> dict[str, PositionPaper]:
    latest: dict[str, PositionPaper] = {}
    for position in sorted(positions, key=lambda item: item.position_id):
        latest[position.analyst_role] = position
    return latest


def _ranking(latest: Sequence[PositionPaper]) -> list[tuple[str, float]]:
    totals: dict[str, list[float]] = {}
    for position in latest:
        totals.setdefault(position.hypothesis_ref, []).append(position.confidence)
    averaged = [
        (ref, sum(values) / len(values)) for ref, values in sorted(totals.items())
    ]
    return sorted(averaged, key=lambda item: (-item[1], item[0]))


class CouncilJudge:
    """按冻结证据与量表产出裁决。"""

    def __init__(
        self, *, frozen_evidence_ids: Collection[str], rubric_version: str
    ) -> None:
        self._frozen_evidence_ids = frozenset(frozen_evidence_ids)
        self._rubric_version = rubric_version

    def _assert_frozen(self, positions: Sequence[PositionPaper], beliefs: Sequence[BeliefUpdate], objections: Sequence[Objection]) -> None:
        referenced: set[str] = set()
        for position in positions:
            referenced.update(position.evidence_ids)
        for belief in beliefs:
            referenced.update(belief.evidence_ids)
        for objection in objections:
            referenced.update(objection.required_evidence)
        missing = sorted(referenced - self._frozen_evidence_ids)
        if missing:
            raise EvidenceNotFrozenError(
                "council decision referenced evidence outside the frozen ledger: "
                + ", ".join(missing)
            )

    def judge(
        self,
        *,
        case_file: CaseFile,
        agenda_items: Sequence[AgendaItem],
        positions: Sequence[PositionPaper],
        objections: Sequence[Objection],
        belief_updates: Sequence[BeliefUpdate],
        rebuttals: Sequence[Rebuttal] = (),
    ) -> CouncilDecision:
        self._assert_frozen(positions, belief_updates, objections)
        _ = agenda_items
        _ = rebuttals

        latest_map = _latest_by_role(positions)
        latest = list(latest_map.values())
        ranking = _ranking(latest)

        if ranking:
            selected_ref, top_confidence = ranking[0]
            selected_refs = [selected_ref]
            rejected_refs = [ref for ref, _ in ranking[1:]]
        else:
            selected_ref, top_confidence = "", 0.0
            selected_refs = []
            rejected_refs = []

        resolved = sorted(
            objection.objection_id
            for objection in objections
            if objection.status in {"answered", "accepted"}
        )
        unresolved = sorted(
            objection.objection_id
            for objection in objections
            if objection.status in {"open", "unresolved"}
        )
        open_high = [
            objection.objection_id
            for objection in objections
            if objection.status == "open" and objection.severity == "high"
        ]

        minority = sorted(
            {
                f"{position.analyst_role} 保留 {position.hypothesis_ref}"
                f"（置信度 {position.confidence:.2f}）"
                for position in latest
                if position.hypothesis_ref != selected_ref
            }
        )
        invalidators = sorted(
            {
                disconfirmer
                for position in latest
                if position.hypothesis_ref == selected_ref
                for disconfirmer in position.disconfirmers
            }
        )

        criterion_scores: dict[str, float] = {
            "alternative_coverage": (
                len(ranking) / len(latest) if latest else 0.0
            ),
            "evidence_backing": (
                sum(1 for position in latest if position.evidence_ids) / len(latest)
                if latest
                else 0.0
            ),
            "objection_closure": (
                len(resolved) / (len(resolved) + len(unresolved))
                if (resolved or unresolved)
                else 1.0
            ),
            "top_confidence": round(top_confidence, 6),
        }

        decision: DecisionKind
        if not latest:
            decision = "insufficient"
            summary = "案卷没有任何立场，无法裁决"
        elif open_high:
            decision = "insufficient"
            summary = (
                f"存在未解决高严重度异议 {', '.join(sorted(open_high))}，"
                "保留少数意见并标记证据不足"
            )
        else:
            decision = "accept"
            summary = f"按量表选择解释力最高的假设 {selected_ref}"

        return CouncilDecision(
            decision_id=(
                f"cd_{case_file.case_id}_v{case_file.case_version}"
                f"_{selected_ref.split(':', 1)[0] if selected_ref else 'none'}"
            ),
            issue_id=(
                f"issue_{selected_ref.split(':', 1)[0]}" if selected_ref else "issue_council"
            ),
            rubric_version=self._rubric_version,
            selected_hypothesis_refs=selected_refs,
            rejected_hypothesis_refs=rejected_refs,
            criterion_scores=criterion_scores,
            reasoning_summary=summary,
            resolved_objection_ids=resolved,
            unresolved_objection_ids=unresolved,
            minority_opinions=minority,
            invalidators=invalidators,
            decision=decision,
            confidence=max(0.0, min(1.0, top_confidence)),
            created_at=case_file.as_of,
        )
