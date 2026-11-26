"""收敛门禁：只依据结构化信号决定继续研究、继续讨论或进入裁决。

继续条件（设计规格 §2.9）：新增有效证据、未解决高严重度异议、假设拓扑
变化或实质性信念变化。否则该轮标记 `no_progress`，按 profile 停止。
异议必须指向具体 ID 并声明击穿条件，缺字段一律拒绝。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.deliberation import Objection
from ..graph.profiles import get_profile

NextAction = Literal[
    "targeted_research", "next_round", "judge", "insufficient", "escalation"
]

MATERIAL_BELIEF_DELTA = 0.05


class InvalidObjectionError(ValueError):
    """异议缺 target_id / severity / kill_criterion 时抛出。"""


class ConvergenceDecision(V4Contract):
    """收敛判定结果；`next_action` 供图路由直接使用。"""

    has_progress: bool
    continue_round: bool
    next_action: NextAction
    stop_reason: str | None = None
    ranking_changed: bool = False
    max_belief_delta: float = Field(ge=0.0)


def validate_objection(objection: Objection) -> None:
    """拒绝缺少目标、严重度或击穿条件的异议。"""
    if not objection.target_id.strip():
        raise InvalidObjectionError(f"objection {objection.objection_id} missing target_id")
    if not str(objection.severity).strip():
        raise InvalidObjectionError(f"objection {objection.objection_id} missing severity")
    if not objection.kill_criterion.strip():
        raise InvalidObjectionError(
            f"objection {objection.objection_id} missing kill_criterion"
        )


def validate_objections(objections: Sequence[Objection]) -> list[Objection]:
    """批量校验并按 objection_id 稳定排序。"""
    for objection in objections:
        validate_objection(objection)
    return sorted(objections, key=lambda item: item.objection_id)


def evaluate_convergence(
    *,
    profile: str,
    round_no: int,
    new_evidence_count: int,
    open_high_objection_ids: Sequence[str],
    pending_gap_ids: Sequence[str],
    ranking_changed: bool,
    topology_changed: bool,
    max_belief_delta: float,
    max_deliberation_rounds: int | None = None,
    material_belief_delta: float = MATERIAL_BELIEF_DELTA,
    research_budget_remaining: int | None = None,
    escalation_required: bool = False,
) -> ConvergenceDecision:
    """按固定顺序判定本轮是否取得有效进展。"""
    max_rounds = (
        get_profile(profile).max_deliberation_rounds
        if max_deliberation_rounds is None
        else max_deliberation_rounds
    )

    if escalation_required:
        return ConvergenceDecision(
            has_progress=False,
            continue_round=False,
            next_action="escalation",
            stop_reason="escalation_required",
            ranking_changed=ranking_changed,
            max_belief_delta=max_belief_delta,
        )

    if max_rounds <= 0:
        return ConvergenceDecision(
            has_progress=False,
            continue_round=False,
            next_action="insufficient",
            stop_reason="profile_disallows_deliberation",
            ranking_changed=ranking_changed,
            max_belief_delta=max_belief_delta,
        )

    # R0 是默认的独立立论轮；只要 profile 允许，必然进入一轮观点交换。
    if round_no == 0:
        return ConvergenceDecision(
            has_progress=True,
            continue_round=True,
            next_action="next_round",
            ranking_changed=ranking_changed,
            max_belief_delta=max_belief_delta,
        )

    has_progress = bool(
        new_evidence_count > 0
        or open_high_objection_ids
        or topology_changed
        or ranking_changed
        or max_belief_delta >= material_belief_delta
    )

    if not has_progress:
        return ConvergenceDecision(
            has_progress=False,
            continue_round=False,
            next_action="judge",
            stop_reason="no_progress",
            ranking_changed=ranking_changed,
            max_belief_delta=max_belief_delta,
        )

    if pending_gap_ids and research_budget_remaining != 0:
        return ConvergenceDecision(
            has_progress=True,
            continue_round=True,
            next_action="targeted_research",
            ranking_changed=ranking_changed,
            max_belief_delta=max_belief_delta,
        )

    if round_no < max_rounds:
        return ConvergenceDecision(
            has_progress=True,
            continue_round=True,
            next_action="next_round",
            ranking_changed=ranking_changed,
            max_belief_delta=max_belief_delta,
        )

    return ConvergenceDecision(
        has_progress=True,
        continue_round=False,
        next_action="judge",
        stop_reason="max_deliberation_rounds",
        ranking_changed=ranking_changed,
        max_belief_delta=max_belief_delta,
    )
