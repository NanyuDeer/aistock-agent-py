"""确定性协作路由。

路由器只读显式特征（材料性、假设分数、证据冲突、跨层冲突、剩余预算、
专业缺口、发布状态），按固定顺序选择四种协作模式。模型建议只能写入
`suggested_mode`，不得覆盖 `mode`。

规则顺序（先命中先返回）：
1. `alert_hot` / `verify` 永不启动议会；
2. 高材料性且准备发布或反证覆盖不足 -> `MANDATORY_RED_TEAM`；
3. 两个高材料性竞争假设接近 / 证据冲突 / 跨层冲突 -> `ANALYST_COUNCIL`；
   预算不足时降级为 `LEAD_WITH_CONSULTANTS`；
4. 明确专业缺口 -> `LEAD_WITH_CONSULTANTS`；
5. 其余 -> `SOLO_DEEP`。
"""

from collections.abc import Sequence
from typing import Literal

from ..contracts.deliberation import CollaborationDecision, Materiality
from .roles import JUDGE_ROLE, LEAD_ROLE, select_roles

CollaborationMode = Literal[
    "SOLO_DEEP", "LEAD_WITH_CONSULTANTS", "ANALYST_COUNCIL", "MANDATORY_RED_TEAM"
]

#: 议会模式的最低预算；低于此值降级为带顾问的单人深研。
COUNCIL_MIN_BUDGET = 20_000
#: 前两名假设分差不超过该值视为“接近”。
CLOSE_MARGIN = 0.08
#: 第一名必须达到的最低材料性得分。
MIN_TOP_SCORE = 0.40
#: 证据冲突分阈值。
CONFLICT_THRESHOLD = 0.60
#: 反证覆盖低于该值时，高材料性结论必须过红队。
COUNTER_EVIDENCE_FLOOR = 0.50

_NO_COUNCIL_PROFILES: frozenset[str] = frozenset({"alert_hot", "verify"})


class CollaborationRoute(CollaborationDecision):
    """确定性路由结果；在既有契约上只追加可解释的 `rationale`。"""

    rationale: str


def _close_competing(scores: Sequence[float]) -> bool:
    if len(scores) < 2:
        return False
    top, second = scores[0], scores[1]
    return top >= MIN_TOP_SCORE and (top - second) <= CLOSE_MARGIN


def route_collaboration(
    *,
    profile: str = "market_trace",
    materiality: Materiality = "medium",
    hypothesis_scores: Sequence[float] = (),
    evidence_conflict_score: float = 0.0,
    cross_layer_conflict: bool = False,
    remaining_budget: int = COUNCIL_MIN_BUDGET,
    professional_gaps: int = 0,
    publish_ready: bool = False,
    counter_evidence_coverage: float = 1.0,
    deadline_pressure: bool = False,
    suggested_mode: str | None = None,
) -> CollaborationRoute:
    """按固定优先级返回协作模式与席位。"""
    scores = sorted((float(score) for score in hypothesis_scores), reverse=True)

    mode: CollaborationMode
    if profile in _NO_COUNCIL_PROFILES:
        mode = "SOLO_DEEP"
        triggers = [f"profile_{profile}_no_council"]
        rationale = f"profile={profile} 按硬规则禁止启动议会"
    elif materiality == "high" and (
        publish_ready or counter_evidence_coverage < COUNTER_EVIDENCE_FLOOR
    ):
        mode = "MANDATORY_RED_TEAM"
        triggers = ["high_materiality_red_team"]
        if publish_ready:
            triggers.append("publish_ready")
        if counter_evidence_coverage < COUNTER_EVIDENCE_FLOOR:
            triggers.append("counter_evidence_insufficient")
        rationale = "高材料性结论在发布前必须经过红队击穿测试"
    else:
        close_competing = _close_competing(scores)
        council_signals: list[str] = []
        if close_competing:
            council_signals.append("close_competing_hypotheses")
        if evidence_conflict_score >= CONFLICT_THRESHOLD:
            council_signals.append("evidence_conflict")
        if cross_layer_conflict:
            council_signals.append("cross_layer_conflict")

        if council_signals:
            if remaining_budget < COUNCIL_MIN_BUDGET or deadline_pressure:
                mode = "LEAD_WITH_CONSULTANTS"
                triggers = ["budget_insufficient_for_council", *council_signals]
                rationale = "议会信号成立，但预算/时限不足，降级为带顾问的单人深研"
            else:
                mode = "ANALYST_COUNCIL"
                triggers = council_signals
                rationale = "存在接近的竞争解释或跨层冲突，启动案卷制议会"
        elif professional_gaps > 0:
            mode = "LEAD_WITH_CONSULTANTS"
            triggers = ["professional_gap"]
            rationale = "主链基本成立但存在明确专业缺口，定向派发咨询"
        else:
            mode = "SOLO_DEEP"
            triggers = ["narrow_question"]
            rationale = "问题窄且证据方向一致，由主分析师独立维护假设图"

    selected_roles = select_roles(mode, cross_layer_conflict=cross_layer_conflict)
    is_council = mode == "ANALYST_COUNCIL"
    return CollaborationRoute(
        mode=mode,
        suggested_mode=suggested_mode,
        trigger_codes=triggers,
        selected_roles=selected_roles,
        lead_role=LEAD_ROLE,
        judge_role=JUDGE_ROLE,
        max_objection_rounds=2 if is_council else (1 if mode == "MANDATORY_RED_TEAM" else 0),
        max_rebuttal_rounds=1 if is_council else 0,
        human_required=False,
        rationale=rationale,
    )
