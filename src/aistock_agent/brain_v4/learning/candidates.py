"""方法候选：永远是草稿，永不直接写生产方法。

硬约束（Task 10 Step 2）：
- `MethodCandidate.draft` 恒为 `True`，`applied_to_production` 恒为 `False`；
- 只有先补齐回放案例集，才能从 `draft` 进入 `replay_passed`；
- 只有先补齐配对 A/B comparison，才能从 `replay_passed` 进入 `ab_passed`；
- `ab_passed` 仅表示“可提交人工复核”，本模块**不提供**任何写生产方法的 API；
- 状态推进通过纯函数返回新对象，不改写入参。
"""

from typing import Literal

from pydantic import Field, model_validator

from aistock_agent.brain_v4.contracts import V4Contract

CandidateStatus = Literal["draft", "replay_passed", "ab_passed", "rejected"]

#: 必须已具备回放证据的阶段。
_REPLAY_REQUIRED: frozenset[CandidateStatus] = frozenset(
    {"replay_passed", "ab_passed"}
)


class CandidateTransitionError(ValueError):
    """候选状态推进不满足前置条件。"""


class MethodCandidate(V4Contract):
    """一个方法改进候选；永远处于草稿态，只能停在人工复核前。"""

    candidate_id: str
    feature_id: str
    method_version: str
    change_summary: str
    rationale: str
    source_run_ids: list[str] = Field(default_factory=list)
    status: CandidateStatus = "draft"
    # 结构上锁死：草稿标记恒为真，生产标记恒为假。
    draft: Literal[True] = True
    applied_to_production: Literal[False] = False
    replay_case_ids: list[str] = Field(default_factory=list)
    ab_comparison_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _require_evidence_for_status(self) -> "MethodCandidate":
        if self.status in _REPLAY_REQUIRED and not self.replay_case_ids:
            raise ValueError(f"状态 {self.status} 必须附带 replay_case_ids")
        if self.status == "ab_passed" and not self.ab_comparison_ids:
            raise ValueError("状态 ab_passed 必须附带 ab_comparison_ids")
        return self


def create_candidate(
    *,
    candidate_id: str,
    feature_id: str,
    method_version: str,
    change_summary: str,
    rationale: str,
    source_run_ids: list[str] | None = None,
) -> MethodCandidate:
    """创建草稿候选；不存在“直接创建为已通过”的入口。"""
    return MethodCandidate(
        candidate_id=candidate_id,
        feature_id=feature_id,
        method_version=method_version,
        change_summary=change_summary,
        rationale=rationale,
        source_run_ids=list(source_run_ids or []),
        status="draft",
    )


def record_replay(
    candidate: MethodCandidate, *, replay_case_ids: list[str]
) -> MethodCandidate:
    """登记回放结果并把草稿推进到 `replay_passed`；立即返回新对象。"""
    if candidate.status != "draft":
        raise CandidateTransitionError(
            f"只有 draft 才能登记回放，当前状态为 {candidate.status}"
        )
    if not replay_case_ids:
        raise CandidateTransitionError("登记回放必须提供至少一个 replay_case_id")
    return candidate.model_copy(
        update={
            "status": "replay_passed",
            "replay_case_ids": sorted(set(replay_case_ids)),
        }
    )


def record_ab(
    candidate: MethodCandidate, *, ab_comparison_ids: list[str]
) -> MethodCandidate:
    """登记配对 A/B 结果并把 `replay_passed` 推进到 `ab_passed`。"""
    if candidate.status != "replay_passed":
        raise CandidateTransitionError(
            f"必须先通过回放才能登记 A/B，当前状态为 {candidate.status}"
        )
    if not ab_comparison_ids:
        raise CandidateTransitionError("登记 A/B 必须提供至少一个 comparison_id")
    return candidate.model_copy(
        update={
            "status": "ab_passed",
            "ab_comparison_ids": sorted(set(ab_comparison_ids)),
        }
    )


def reject_candidate(candidate: MethodCandidate, *, reason: str) -> MethodCandidate:
    """人工复核否决：保留全部证据，仅标记状态与理由。"""
    if not reason:
        raise CandidateTransitionError("否决候选必须给出理由")
    return candidate.model_copy(
        update={
            "status": "rejected",
            "rationale": f"{candidate.rationale} | rejected: {reason}",
        }
    )


__all__ = [
    "CandidateStatus",
    "CandidateTransitionError",
    "MethodCandidate",
    "create_candidate",
    "record_ab",
    "record_replay",
    "reject_candidate",
]
