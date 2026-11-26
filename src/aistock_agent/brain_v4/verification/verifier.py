"""到期验证：append-only 验证记录，方向与主因分离判定。

硬约束（Task 10 Step 2）：
- 验证器只读取原 `V4Judgment`，**绝不修改**它，也不改写研究账本或证据；
- 一条验证结果一旦追加便不可变，历史记录不被后续验证覆盖；
- 方向（direction）与主因（cause）分开判定，便于区分“看对方向但归错因”；
- 全部时间边界来自 judgment 与显式 observed 输入，不读系统时间、不触网。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Literal

from pydantic import ConfigDict, Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.judgment import V4Judgment

from .error_tags import ErrorTag, classify_error

VerificationVerdict = Literal["correct", "partially_correct", "wrong"]


class ObservedOutcome(V4Contract):
    """事后观测到的真实方向与主因；由验证者显式提供，不来自 V4 自身。"""

    direction: str
    primary_cause: str | None = None
    observed_at: datetime


class VerificationRecord(V4Contract):
    """一条不可变的到期验证记录；只引用原 judgment，不携带其可变状态。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    verification_id: str
    judgment_id: str
    run_id: str
    feature_id: str
    snapshot_id: str
    as_of: datetime
    data_cutoff: datetime
    expected_direction: str
    observed_direction: str
    direction_match: bool
    expected_primary_cause: str | None = None
    observed_primary_cause: str | None = None
    cause_match: bool
    confidence: float
    verdict: VerificationVerdict
    error_tags: list[ErrorTag] = Field(default_factory=list)
    observed_at: datetime


def _digest_verification_id(
    judgment_id: str, observed: ObservedOutcome
) -> str:
    payload = (
        f"{judgment_id}|{observed.direction}|{observed.primary_cause}|"
        f"{observed.observed_at.isoformat()}"
    )
    return f"ver_{hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]}"


def _verdict(direction_match: bool, cause_match: bool) -> VerificationVerdict:
    if not direction_match:
        return "wrong"
    if not cause_match:
        return "partially_correct"
    return "correct"


def verify_judgment(
    judgment: V4Judgment,
    *,
    expected_direction: str,
    expected_primary_cause: str | None,
    observed: ObservedOutcome,
    evidence_depth: int | None = None,
    unsupported_edge_ratio: float = 0.0,
    time_leakage: bool = False,
    min_evidence_depth: int = 2,
    max_unsupported_edge_ratio: float = 0.3,
    high_confidence_threshold: float = 0.7,
) -> VerificationRecord:
    """在到期后判定一条 judgment；只读原对象，返回新的不可变记录。"""
    direction_match = judgment.decision not in {"insufficient", "escalate"} and (
        str(expected_direction) == str(observed.direction)
    )
    # 方向都不对时，主因自然也不成立；方向对时才独立比较主因。
    cause_match = direction_match and (
        expected_primary_cause is not None
        and observed.primary_cause is not None
        and str(expected_primary_cause) == str(observed.primary_cause)
    )
    insufficient_missed = judgment.status == "insufficient" and not direction_match

    tags = classify_error(
        direction_match=direction_match,
        cause_match=cause_match,
        confidence=judgment.confidence,
        high_confidence_threshold=high_confidence_threshold,
        time_leakage=time_leakage,
        evidence_depth=evidence_depth,
        min_evidence_depth=min_evidence_depth,
        unsupported_edge_ratio=unsupported_edge_ratio,
        max_unsupported_edge_ratio=max_unsupported_edge_ratio,
        insufficient_missed=insufficient_missed,
    )

    return VerificationRecord(
        verification_id=_digest_verification_id(judgment.judgment_id, observed),
        judgment_id=judgment.judgment_id,
        run_id=judgment.run_id,
        feature_id=judgment.profile,
        snapshot_id=judgment.snapshot_id,
        as_of=judgment.as_of,
        data_cutoff=judgment.data_cutoff,
        expected_direction=expected_direction,
        observed_direction=observed.direction,
        direction_match=direction_match,
        expected_primary_cause=expected_primary_cause,
        observed_primary_cause=observed.primary_cause,
        cause_match=cause_match,
        confidence=judgment.confidence,
        verdict=_verdict(direction_match, cause_match),
        error_tags=list(tags),
        observed_at=observed.observed_at,
    )


class Verifier:
    """append-only 验证器：每条记录追加后不可变，历史不被覆盖。"""

    def __init__(self) -> None:
        self._records: list[VerificationRecord] = []

    def verify(
        self,
        judgment: V4Judgment,
        *,
        expected_direction: str,
        expected_primary_cause: str | None,
        observed: ObservedOutcome,
        evidence_depth: int | None = None,
        unsupported_edge_ratio: float = 0.0,
        time_leakage: bool = False,
        min_evidence_depth: int = 2,
        max_unsupported_edge_ratio: float = 0.3,
        high_confidence_threshold: float = 0.7,
    ) -> VerificationRecord:
        record = verify_judgment(
            judgment,
            expected_direction=expected_direction,
            expected_primary_cause=expected_primary_cause,
            observed=observed,
            evidence_depth=evidence_depth,
            unsupported_edge_ratio=unsupported_edge_ratio,
            time_leakage=time_leakage,
            min_evidence_depth=min_evidence_depth,
            max_unsupported_edge_ratio=max_unsupported_edge_ratio,
            high_confidence_threshold=high_confidence_threshold,
        )
        self._records.append(record)
        return record

    @property
    def records(self) -> tuple[VerificationRecord, ...]:
        """只读快照；调用方无法通过返回值改写内部历史。"""
        return tuple(self._records)


__all__ = [
    "ObservedOutcome",
    "VerificationRecord",
    "VerificationVerdict",
    "Verifier",
    "verify_judgment",
]
