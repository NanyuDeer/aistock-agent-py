"""错误标签分类：方向与主因分离，高置信 miss 追加过度自信。

标签语义（Task 10 Step 2）：
- `direction_wrong`：方向判错；
- `cause_wrong`：方向正确但主因错误（**不得**同时出现 `direction_wrong`）；
- `overconfidence`：高置信 miss（方向判错且置信度不低于阈值）；
- `time_leakage`：使用了 cutoff 之后才可知的信息；
- `shallow_evidence`：支撑证据深度不足；
- `unsupported_edge`：因果路径存在无证据支撑的边；
- `insufficient_missed`：本可给出结论却判为 insufficient。

本模块是纯函数，不读系统时间、不触网、不修改任何输入。
"""

from collections.abc import Sequence
from typing import Literal

ErrorTag = Literal[
    "direction_wrong",
    "cause_wrong",
    "overconfidence",
    "time_leakage",
    "shallow_evidence",
    "unsupported_edge",
    "insufficient_missed",
]

#: 固定输出顺序，保证同一输入永远得到同一标签序列。
_TAG_ORDER: tuple[ErrorTag, ...] = (
    "direction_wrong",
    "cause_wrong",
    "overconfidence",
    "time_leakage",
    "shallow_evidence",
    "unsupported_edge",
    "insufficient_missed",
)


def classify_error(
    *,
    direction_match: bool,
    cause_match: bool,
    confidence: float,
    high_confidence_threshold: float = 0.7,
    time_leakage: bool = False,
    evidence_depth: int | None = None,
    min_evidence_depth: int = 2,
    unsupported_edge_ratio: float = 0.0,
    max_unsupported_edge_ratio: float = 0.3,
    insufficient_missed: bool = False,
) -> tuple[ErrorTag, ...]:
    """按显式信号产出错误标签；`cause_wrong` 与 `direction_wrong` 互斥。"""
    found: set[ErrorTag] = set()

    if not direction_match:
        found.add("direction_wrong")
    elif not cause_match:
        # 方向正确但主因错误：只标 cause_wrong，不误标 direction_wrong。
        found.add("cause_wrong")

    if not direction_match and confidence >= high_confidence_threshold:
        found.add("overconfidence")

    if time_leakage:
        found.add("time_leakage")

    if evidence_depth is not None and evidence_depth < min_evidence_depth:
        found.add("shallow_evidence")

    if unsupported_edge_ratio > max_unsupported_edge_ratio:
        found.add("unsupported_edge")

    if insufficient_missed:
        found.add("insufficient_missed")

    return tuple(tag for tag in _TAG_ORDER if tag in found)


def ordered_tags(tags: Sequence[str]) -> tuple[ErrorTag, ...]:
    """把任意标签集合归一到固定顺序，供记录持久化使用。"""
    known = {tag for tag in tags if tag in _TAG_ORDER}
    return tuple(tag for tag in _TAG_ORDER if tag in known)


__all__ = ["ErrorTag", "classify_error", "ordered_tags"]
