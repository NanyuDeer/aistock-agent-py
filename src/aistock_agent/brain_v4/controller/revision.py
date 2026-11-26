"""局部回改决策：把矛盾证据转成可落账的 `RevisionDecision`。

设计规格 §5.2/§5.3：
- 正式回改动作是 `add_evidence | reweight | revise | split | merge | reject`；
- 单假设最多回改 2 次，单 run 最多 4 次（run 级预算由编排方另管）；
- A→B→A 振荡必须标记 unstable；
- 回改只解锁下游，必须复用 `impacted_refs` 做局部影响分析。

本模块的动作集合使用计划测试断言的 `revise_hypothesis`，落 `RevisionEvent`
时再映射到既有契约的 `revise`（见 `build_revision_event`）。新假设的陈述
必须来自矛盾证据 fixture，而不是对文本做关键字匹配猜结论。
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.research import CausalEdge, Hypothesis, RevisionAction, RevisionEvent
from .impact import impacted_refs

RevisionDecisionAction = Literal[
    "add_evidence",
    "reweight",
    "revise_hypothesis",
    "split",
    "merge",
    "reject",
]

#: 决策动作 → 既有 `RevisionAction`。`revise_hypothesis` 落账时统一写成 `revise`，
#: 避免公共契约因本 Task 的动作命名而扩容。
_EVENT_ACTION_MAP: dict[str, RevisionAction] = {
    "add_evidence": "add_evidence",
    "reweight": "reweight",
    "revise_hypothesis": "revise",
    "split": "split",
    "merge": "merge",
    "reject": "reject",
}


class Contradiction(V4Contract):
    """一条需要触发回改的结构化矛盾。

    `suggested_statement` 由证据层给出，而不是由控制器从文本里猜；这样同一
    矛盾 fixture 回放时一定得到同一新假设陈述。
    """

    contradiction_id: str
    target_ref: str
    observed_pattern: str
    suggested_statement: str
    suggested_assumptions: list[str] = Field(default_factory=list)
    suggested_disconfirmers: list[str] = Field(default_factory=list)
    triggering_evidence_ids: list[str] = Field(default_factory=list)
    reason: str


class RevisionDecision(V4Contract):
    """一次回改裁决：动作、新版本假设、局部影响与振荡标记。

    `from_statement` / `to_statement` 显式记录改口前后，供后续历史判断
    A→B→A 振荡，不依赖字符串包含关系。
    """

    action: RevisionDecisionAction
    target_ref: str
    new_hypothesis: Hypothesis | None = None
    impacted_hypothesis_refs: list[str] = Field(default_factory=list)
    impacted_claim_ids: list[str] = Field(default_factory=list)
    reason: str
    revision_no: int = Field(ge=0)
    oscillation: bool = False
    from_statement: str
    to_statement: str
    triggering_evidence_ids: list[str] = Field(default_factory=list)


def _stem(ref: str) -> str:
    """取 `id:v{n}` 的主干，用于判断历史回改是否属于同一假设。"""
    return ref.split(":", 1)[0]


def _same_hypothesis(left: str, right: str) -> bool:
    return _stem(left) == _stem(right)


def _detect_oscillation(
    hypothesis: Hypothesis,
    prior: Sequence[RevisionDecision],
    proposed_statement: str,
) -> bool:
    """检测 A→B→A：本次目标陈述等于上一次回改的起点，且当前处于上次终点。"""
    if not prior:
        return False
    latest = prior[-1]
    return (
        latest.to_statement == hypothesis.statement
        and latest.from_statement == proposed_statement
        and latest.to_statement != latest.from_statement
    )


def _revised_confidence(prior: float) -> float:
    """根因降级为放大器时降低先验置信；固定步长保证可复现。"""
    return max(0.0, prior - 0.2)


def revise(
    hypothesis: Hypothesis,
    contradictions: Sequence[Contradiction],
    graph: Sequence[CausalEdge],
    history: Sequence[RevisionDecision] | None = None,
    max_revisions_per_hypothesis: int = 2,
) -> RevisionDecision:
    """根据矛盾证据输出回改决策。

    判定顺序固定为：先查单假设回改预算，再计算影响与振荡。预算拒绝不产生
    新假设，避免第 3 次改口覆盖已落账的历史。
    """
    prior_history = [item for item in (history or []) if _same_hypothesis(item.target_ref, hypothesis.ref)]
    revision_no = len(prior_history) + 1
    impact = impacted_refs(graph, hypothesis.ref)

    if len(prior_history) >= max_revisions_per_hypothesis:
        return RevisionDecision(
            action="reject",
            target_ref=hypothesis.ref,
            reason=f"超过单假设最大回改次数 {max_revisions_per_hypothesis}，拒绝第 {revision_no} 次回改",
            revision_no=revision_no,
            oscillation=False,
            from_statement=hypothesis.statement,
            to_statement=hypothesis.statement,
        )

    if not contradictions:
        return RevisionDecision(
            action="add_evidence",
            target_ref=hypothesis.ref,
            reason="未提供矛盾证据，转为定向补证",
            revision_no=revision_no,
            oscillation=False,
            from_statement=hypothesis.statement,
            to_statement=hypothesis.statement,
        )

    # 多条矛盾时按 contradiction_id 稳定排序后取第一条，保证同输入同决策。
    contradiction = sorted(contradictions, key=lambda item: item.contradiction_id)[0]
    proposed_statement = contradiction.suggested_statement
    oscillation = _detect_oscillation(hypothesis, prior_history, proposed_statement)

    new_hypothesis = Hypothesis(
        hypothesis_id=hypothesis.hypothesis_id,
        version=hypothesis.version + 1,
        statement=proposed_statement,
        status="weakened",
        prior_confidence=_revised_confidence(hypothesis.prior_confidence),
        assumptions=contradiction.suggested_assumptions or hypothesis.assumptions,
        disconfirmers=contradiction.suggested_disconfirmers or hypothesis.disconfirmers,
        required_evidence_types=hypothesis.required_evidence_types,
        parent_ids=[*hypothesis.parent_ids, hypothesis.ref],
        scope=hypothesis.scope,
    )

    return RevisionDecision(
        action="revise_hypothesis",
        target_ref=hypothesis.ref,
        new_hypothesis=new_hypothesis,
        impacted_hypothesis_refs=impact.impacted_hypothesis_refs,
        impacted_claim_ids=impact.impacted_claim_ids,
        reason=contradiction.reason,
        revision_no=revision_no,
        oscillation=oscillation,
        from_statement=hypothesis.statement,
        to_statement=proposed_statement,
        triggering_evidence_ids=contradiction.triggering_evidence_ids,
    )


def build_revision_event(
    decision: RevisionDecision,
    *,
    revision_id: str,
    run_id: str,
    case_version: int,
    created_at: datetime,
) -> RevisionEvent:
    """把回改决策落成正式 `RevisionEvent`。

    `created_at` 必须由调用方显式注入，节点不得读系统时间。`revise_hypothesis`
    在这里映射为公共契约既有的 `revise`。
    """
    target_refs = [decision.target_ref]
    if decision.new_hypothesis is not None:
        target_refs.append(decision.new_hypothesis.ref)
    return RevisionEvent(
        revision_id=revision_id,
        run_id=run_id,
        action=_EVENT_ACTION_MAP[decision.action],
        target_refs=target_refs,
        triggering_evidence_ids=decision.triggering_evidence_ids,
        impacted_hypothesis_refs=decision.impacted_hypothesis_refs,
        impacted_claim_ids=decision.impacted_claim_ids,
        reason=decision.reason,
        case_version=case_version,
        created_at=created_at,
    )
