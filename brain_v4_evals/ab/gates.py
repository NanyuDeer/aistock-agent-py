"""灰度晋级门禁：全部工程与质量门禁真实通过才推荐晋级。

门禁（Task 10 Step 3）：
- 时间泄漏为零（`time_leakage_count == 0`）；
- snapshot SHA 一致（`snapshot_mismatch_count == 0`）；
- 无生产污染（`production_contamination_count == 0`）；
- 最小样本量（默认 20）；
- 质量阈值（`quality_delta` 不低于下限）；
- P95 与 cost 回退可控（相对回退不超过阈值）；
- 回滚已测试（`rollback_tested=True`）。

`p95_delta` / `cost_delta` 均按 `(new - old) / old` 的相对回退量传入，负值代表改善。
任一门禁不通过即 `promote=False`，`reason` 返回优先级最高的失败原因，
`reasons` 保留全部失败原因，便于定位。
"""

from pydantic import Field

from aistock_agent.brain_v4.contracts import V4Contract

#: 失败原因的固定优先级顺序（先命中先作为主 reason）。
_REASON_PRIORITY: tuple[str, ...] = (
    "time_leakage",
    "snapshot_mismatch",
    "production_contamination",
    "sample_too_small",
    "quality_below_threshold",
    "p95_regression",
    "cost_regression",
    "rollback_untested",
)


class PromotionResult(V4Contract):
    """一次灰度晋级评估结果。"""

    promote: bool
    reason: str
    reasons: list[str] = Field(default_factory=list)


def evaluate_promotion(
    *,
    sample_size: int,
    quality_delta: float,
    p95_delta: float,
    min_sample_size: int = 20,
    min_quality_delta: float = 0.0,
    max_p95_regression_ratio: float = 0.10,
    max_cost_regression_ratio: float = 0.20,
    cost_delta: float = 0.0,
    time_leakage_count: int = 0,
    snapshot_mismatch_count: int = 0,
    production_contamination_count: int = 0,
    rollback_tested: bool = False,
) -> PromotionResult:
    """评估是否允许灰度晋级；返回明确的通过/拒绝原因。"""
    failures: set[str] = set()

    if time_leakage_count > 0:
        failures.add("time_leakage")
    if snapshot_mismatch_count > 0:
        failures.add("snapshot_mismatch")
    if production_contamination_count > 0:
        failures.add("production_contamination")
    if sample_size < min_sample_size:
        failures.add("sample_too_small")
    if quality_delta < min_quality_delta:
        failures.add("quality_below_threshold")
    if p95_delta > max_p95_regression_ratio:
        failures.add("p95_regression")
    if cost_delta > max_cost_regression_ratio:
        failures.add("cost_regression")
    if not rollback_tested:
        failures.add("rollback_untested")

    ordered = [reason for reason in _REASON_PRIORITY if reason in failures]
    if not ordered:
        return PromotionResult(promote=True, reason="ok", reasons=[])
    return PromotionResult(promote=False, reason=ordered[0], reasons=ordered)


__all__ = ["PromotionResult", "evaluate_promotion"]
