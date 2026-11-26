"""学习层包：方法候选与议会质量指标。

学习层只产出**草稿**方法与统计指标，绝不直接修改生产方法或研判逻辑。
"""

from .candidates import (
    CandidateStatus,
    CandidateTransitionError,
    MethodCandidate,
    create_candidate,
    record_ab,
    record_replay,
    reject_candidate,
)
from .deliberation_metrics import (
    DeliberationInputs,
    DeliberationMetrics,
    measure_deliberation,
)

__all__ = [
    "CandidateStatus",
    "CandidateTransitionError",
    "DeliberationInputs",
    "DeliberationMetrics",
    "MethodCandidate",
    "create_candidate",
    "measure_deliberation",
    "record_ab",
    "record_replay",
    "reject_candidate",
]
