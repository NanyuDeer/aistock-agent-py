"""信念更新、反羊群校验与影响边。

不变量（设计规格 §3.16 / §3.17）：
- 同伴观点不是证据；没有新增 evidence 或机制修正时，置信度不得上调；
- 读取 PeerBrief 的席位即使选择 `hold` 也必须落一条 `BeliefUpdate`；
- 每条影响边显式记录来源产物、目标更新、影响类型与是否证据支撑；
- 保留 prior/posterior 引用与置信度，append-only 不覆盖历史轮次。
"""

from collections.abc import Sequence
from typing import Literal

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.deliberation import BeliefUpdate, InfluenceEdge, PeerBrief

ConfidenceIncreaseReason = Literal[
    "confidence_increase_requires_evidence_or_mechanism_change"
]

CONFIDENCE_INCREASE_REASON: ConfidenceIncreaseReason = (
    "confidence_increase_requires_evidence_or_mechanism_change"
)

_TIMELINE_TOKENS: tuple[str, ...] = ("timeline", "chronology", "time_order")


class BeliefValidation(V4Contract):
    """反羊群校验结果：允许或显式拒绝。"""

    accepted: bool
    reason: ConfidenceIncreaseReason | None = None


class BeliefUpdateResult(BeliefUpdate):
    """`BeliefUpdate` 加本次变化产生的影响边，便于一次落账。"""

    influence_edges: list[InfluenceEdge] = Field(default_factory=list)


def validate_belief_update(
    prior_confidence: float,
    posterior_confidence: float,
    influencing_artifact_ids: Sequence[str],
    evidence_ids: Sequence[str],
    mechanism_changed: bool = False,
) -> BeliefValidation:
    """置信度上调必须由新增证据或机制修正支撑。"""
    # `influencing_artifact_ids` 必须保留在签名中：即使同伴产物很多，
    # 只要没有证据或机制变化，上调一律拒绝。
    _ = influencing_artifact_ids
    if posterior_confidence <= prior_confidence:
        return BeliefValidation(accepted=True)
    if evidence_ids or mechanism_changed:
        return BeliefValidation(accepted=True)
    return BeliefValidation(accepted=False, reason=CONFIDENCE_INCREASE_REASON)


def _has_timeline_conflict(brief: PeerBrief) -> bool:
    return any(
        any(token in disagreement.lower() for token in _TIMELINE_TOKENS)
        for disagreement in brief.material_disagreements
    )


def _clamp(value: float) -> float:
    return max(0.0, min(1.0, value))


def belief_update_from_peer_brief(
    peer_brief: PeerBrief,
    *,
    prior_hypothesis_ref: str | None = None,
    prior_confidence: float = 0.55,
    role: str | None = None,
    round_no: int | None = None,
    case_version: int | None = None,
    issue_id: str | None = None,
) -> BeliefUpdateResult:
    """把同伴摘要转成可审计的信念更新。

    时间线冲突会削弱原假设并请求对应 Gap；其他分歧只记录为 hold，
    不允许仅凭同伴观点上调置信度。
    """
    target_role = role or peer_brief.target_role
    deliberation_round = (
        round_no if round_no is not None else peer_brief.deliberation_round
    )
    effective_case_version = (
        case_version if case_version is not None else peer_brief.case_version
    )
    prior_ref = prior_hypothesis_ref or (
        peer_brief.competing_claims[0]
        if peer_brief.competing_claims
        else f"hypothesis_{target_role}:v1"
    )
    resolved_issue = issue_id or f"issue_{prior_ref.split(':', 1)[0]}"
    update_id = f"bu_r{deliberation_round:02d}_v{effective_case_version:02d}_{target_role}"

    if _has_timeline_conflict(peer_brief):
        action: Literal["hold", "strengthen", "weaken", "switch", "split", "merge"] = (
            "weaken"
        )
        posterior = _clamp(prior_confidence - 0.10)
        requested_gaps = sorted(peer_brief.open_questions)
        reason_summary = "同伴时间线审计发现价格先于消息，削弱原假设并请求补证"
    else:
        action = "hold"
        posterior = prior_confidence
        requested_gaps = []
        reason_summary = "已阅读同伴摘要，但无新增证据或机制修正，保持原判断"

    validation = validate_belief_update(
        prior_confidence,
        posterior,
        peer_brief.source_artifact_ids,
        peer_brief.evidence_ids,
    )
    if not validation.accepted:
        action = "hold"
        posterior = prior_confidence
        requested_gaps = []
        reason_summary = "置信度上调被反羊群校验拒绝，改为 hold"

    influence_edges: list[InfluenceEdge] = []
    if action != "hold" and peer_brief.source_artifact_ids:
        influence_edges.append(
            InfluenceEdge(
                influence_edge_id=f"ie_{update_id}_{peer_brief.source_artifact_ids[0]}",
                source_artifact_id=peer_brief.source_artifact_ids[0],
                target_belief_update_id=update_id,
                target_hypothesis_ref=prior_ref,
                influence_type="search_direction",
                effect="redirect",
                evidence_backed=False,
                materiality="high",
                created_at=peer_brief.generated_at,
            )
        )

    return BeliefUpdateResult(
        belief_update_id=update_id,
        issue_id=resolved_issue,
        analyst_role=target_role,
        case_version=effective_case_version,
        deliberation_round=deliberation_round,
        prior_hypothesis_ref=prior_ref,
        prior_confidence=prior_confidence,
        action=action,
        posterior_hypothesis_refs=[prior_ref],
        posterior_confidence=[posterior],
        influencing_artifact_ids=sorted(peer_brief.source_artifact_ids),
        evidence_ids=sorted(peer_brief.evidence_ids),
        requested_gap_ids=requested_gaps,
        reason_summary=reason_summary,
        influence_edges=influence_edges,
    )


def make_hold_update(
    *,
    role: str,
    issue_id: str,
    case_version: int,
    round_no: int,
    prior_hypothesis_ref: str,
    prior_confidence: float,
) -> BeliefUpdateResult:
    """没有同伴摘要或选择保持时，仍必须落一条 hold 更新。"""
    return BeliefUpdateResult(
        belief_update_id=(
            f"bu_r{round_no:02d}_v{case_version:02d}_{role}"
        ),
        issue_id=issue_id,
        analyst_role=role,
        case_version=case_version,
        deliberation_round=round_no,
        prior_hypothesis_ref=prior_hypothesis_ref,
        prior_confidence=prior_confidence,
        action="hold",
        posterior_hypothesis_refs=[prior_hypothesis_ref],
        posterior_confidence=[prior_confidence],
        reason_summary="无同伴摘要或选择保持，记录 hold",
    )
