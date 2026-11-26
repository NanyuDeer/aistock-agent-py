"""角色裁剪的 PeerBrief 生成。

首轮独立立论（R0）结束前禁止生成；只有 R1+ 才按目标席位裁剪公开产物。
`PeerBrief` 只包含观点、证据引用、分歧与开放问题，不包含原始推理，
也不得进入 `EvidenceLedger`——证据账本只接受 `EvidenceItem`。
"""

from collections.abc import Sequence

from ..contracts.deliberation import (
    AgendaItem,
    CaseFile,
    PeerBrief,
    PositionPaper,
)
from .roles import LEAD_ROLE


def _latest_by_role(submissions: Sequence[PositionPaper]) -> dict[str, PositionPaper]:
    latest: dict[str, PositionPaper] = {}
    for paper in sorted(submissions, key=lambda item: item.position_id):
        latest[paper.analyst_role] = paper
    return latest


def build_peer_briefs(
    *,
    case_file: CaseFile,
    round_no: int,
    active_roles: Sequence[str],
    submissions: Sequence[PositionPaper],
    agenda_items: Sequence[AgendaItem] = (),
    resolved_gap_ids: Sequence[str] = (),
) -> list[PeerBrief]:
    """为每个活跃席位生成最小必要同伴摘要；R0 返回空列表。"""
    if round_no < 1:
        return []

    latest = _latest_by_role(submissions)
    resolved = set(resolved_gap_ids)
    briefs: list[PeerBrief] = []

    for target_role in sorted(active_roles):
        own = latest.get(target_role)
        others = [
            paper
            for role, paper in sorted(latest.items())
            if role != target_role
        ]
        if not others:
            continue

        disagreements: set[str] = set()
        open_questions: set[str] = set()
        evidence_ids: set[str] = set()
        competing_claims: list[str] = []
        source_ids: list[str] = []

        for paper in others:
            source_ids.append(paper.position_id)
            competing_claims.append(f"{paper.analyst_role}:{paper.thesis}")
            evidence_ids.update(paper.evidence_ids)
            if own is not None and paper.hypothesis_ref != own.hypothesis_ref:
                disagreements.add(f"competing_hypothesis:{paper.hypothesis_ref}")
            for unknown in paper.unknowns:
                if unknown.startswith("gap_") and unknown not in resolved:
                    open_questions.add(unknown)

        # 时间线审计只向主分析师报告盘前触发冲突；缺口闭合后不再重复。
        timeline_conflict = (
            target_role == LEAD_ROLE
            and any(paper.analyst_role == "timeline" for paper in others)
            and "gap_pre_move_trigger" not in resolved
        )
        if timeline_conflict:
            disagreements.add("timeline_conflict")

        briefs.append(
            PeerBrief(
                peer_brief_id=(
                    f"pb_r{round_no:02d}_v{case_file.case_version:02d}_{target_role}"
                ),
                case_id=case_file.case_id,
                case_version=case_file.case_version,
                deliberation_round=round_no,
                target_role=target_role,
                source_artifact_ids=sorted(source_ids),
                competing_claims=sorted(competing_claims),
                evidence_ids=sorted(evidence_ids),
                material_disagreements=sorted(disagreements),
                open_questions=sorted(open_questions),
                omitted_sections=["private_scratch", "raw_reasoning"],
                generated_at=case_file.as_of,
            )
        )

    return sorted(briefs, key=lambda item: item.peer_brief_id)
