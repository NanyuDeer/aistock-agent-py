"""结构化充分性门禁：只产出 Gap 与得分，不写叙述文本。

设计规格 §5 / §2.10 要求研究循环围绕结构化缺口定向补证，而不是让模型
自评“我准备好了”。门禁逐项检查证据覆盖、时间顺序、机制、量级、替代
解释、失效条件与无支撑边比例；任何一项不满足都返回 `ResearchGap`。

时间纪律：`as_of` / `data_cutoff` 必须由调用方显式传入，本模块绝不读取
系统时间；同一输入必须得到逐字节一致的 `SufficiencyReport`。
"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Literal

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.evidence import EvidenceItem
from ..contracts.research import CausalEdge, GapType, Hypothesis, ResearchGap

#: 各维度得分，0.0-1.0；供 router 与评测台比较，不参与生成文字。
_DEFAULT_UNSUPPORTED_EDGE_THRESHOLD = 0.5
Materiality = Literal["low", "medium", "high"]


class SufficiencyScores(V4Contract):
    """门禁各维度的确定性得分。"""

    evidence_coverage: float = Field(ge=0.0, le=1.0)
    time_order: float = Field(ge=0.0, le=1.0)
    mechanism: float = Field(ge=0.0, le=1.0)
    magnitude: float = Field(ge=0.0, le=1.0)
    alternative: float = Field(ge=0.0, le=1.0)
    invalidator: float = Field(ge=0.0, le=1.0)


class SufficiencyReport(V4Contract):
    """一次充分性审查的结构化结论。

    `passed` 完全由 `gaps` 推导，模型无法用自评分绕过；`unsupported_edge_ratio`
    单独保留，便于阈值调参和离线评测追溯。
    """

    run_id: str
    issue_id: str
    as_of: datetime
    data_cutoff: datetime
    passed: bool
    gaps: list[ResearchGap] = Field(default_factory=list)
    scores: SufficiencyScores
    unsupported_edge_ratio: float = Field(ge=0.0, le=1.0)


def _ratio(numerator: int, denominator: int, *, empty_value: float = 1.0) -> float:
    """安全比率；空集合按 `empty_value` 处理，避免除零。"""
    if denominator == 0:
        return empty_value
    return numerator / denominator


def _gap(
    *,
    index: int,
    run_id: str,
    issue_id: str,
    gap_type: GapType,
    description: str,
    required_evidence_types: Sequence[str] = (),
    targets: Sequence[str] = (),
    materiality: Materiality = "medium",
) -> ResearchGap:
    """构造结构化缺口；description 只放机器可读标识，不写分析叙述。"""
    return ResearchGap(
        gap_id=f"gap_{gap_type}_{index:03d}",
        run_id=run_id,
        issue_id=issue_id,
        gap_type=gap_type,
        description=description,
        required_evidence_types=list(required_evidence_types),
        target_hypothesis_refs=list(targets),
        materiality=materiality,
        status="open",
    )


def evaluate_sufficiency(
    *,
    run_id: str,
    issue_id: str,
    as_of: datetime,
    data_cutoff: datetime,
    hypotheses: Sequence[Hypothesis],
    evidence: Sequence[EvidenceItem],
    causal_edges: Sequence[CausalEdge],
    required_evidence_types: Mapping[str, Sequence[str]] | None = None,
    unsupported_edge_ratio_threshold: float = _DEFAULT_UNSUPPORTED_EDGE_THRESHOLD,
) -> SufficiencyReport:
    """执行确定性充分性门禁；返回缺口列表与各维度得分。"""
    hypotheses_sorted = sorted(hypotheses, key=lambda item: item.ref)
    evidence_sorted = sorted(evidence, key=lambda item: item.evidence_id)
    edges_sorted = sorted(causal_edges, key=lambda item: item.edge_id)

    gaps: list[ResearchGap] = []

    def add(
        gap_type: GapType,
        description: str,
        *,
        required_evidence_types: Sequence[str] = (),
        targets: Sequence[str] = (),
        materiality: Materiality = "medium",
    ) -> None:
        gaps.append(
            _gap(
                index=len(gaps),
                run_id=run_id,
                issue_id=issue_id,
                gap_type=gap_type,
                description=description,
                required_evidence_types=required_evidence_types,
                targets=targets,
                materiality=materiality,
            )
        )

    # 1) 证据覆盖：每个假设至少被一条证据支持，且必需要证据类型齐全。
    source_types_by_ref: dict[str, set[str]] = {}
    for item in evidence_sorted:
        for ref in item.supports_hypothesis_refs:
            source_types_by_ref.setdefault(ref, set()).add(item.source_type)

    covered = 0
    for hypothesis in hypotheses_sorted:
        required = required_evidence_types.get(hypothesis.ref) if required_evidence_types else None
        required_types = sorted(set(required or hypothesis.required_evidence_types))
        seen_types = source_types_by_ref.get(hypothesis.ref, set())
        missing_types = sorted(set(required_types) - seen_types)
        if hypothesis.ref not in source_types_by_ref:
            add(
                "missing_evidence",
                f"missing_evidence:{hypothesis.ref}",
                required_evidence_types=required_types,
                targets=[hypothesis.ref],
                materiality="high",
            )
            continue
        covered += 1
        if missing_types:
            add(
                "missing_evidence",
                f"missing_evidence_type:{hypothesis.ref}",
                required_evidence_types=missing_types,
                targets=[hypothesis.ref],
                materiality="high",
            )

    # 2) 时间顺序：发布时间晚于 as_of 或事件时间晚于 data_cutoff 的证据不可用。
    time_violations = [
        item
        for item in evidence_sorted
        if item.published_at > as_of or item.event_time > data_cutoff
    ]
    for item in time_violations:
        targets = sorted(
            set(item.supports_hypothesis_refs) | set(item.contradicts_hypothesis_refs)
        )
        add(
            "time_order",
            f"time_order:{item.evidence_id}",
            targets=targets,
            materiality="high",
        )

    # 3) 机制与 4) 量级：因果边必须说明“怎么发生”和“多大”。
    mechanism_violations = [edge for edge in edges_sorted if not edge.mechanism.strip()]
    for edge in mechanism_violations:
        add("mechanism", f"mechanism:{edge.edge_id}")

    magnitude_violations = [edge for edge in edges_sorted if not edge.magnitude_check.strip()]
    for edge in magnitude_violations:
        add("magnitude", f"magnitude:{edge.edge_id}")

    # 5) 替代解释：至少要有两个竞争假设，否则无法排除单一故事。
    alternative_ok = len(hypotheses_sorted) >= 2
    if not alternative_ok:
        add(
            "alternative",
            "alternative:single_hypothesis",
            targets=[hypothesis.ref for hypothesis in hypotheses_sorted],
            materiality="high",
        )

    # 6) 失效条件：反事实检查为空意味着无法被证伪。
    invalidator_violations = [
        edge for edge in edges_sorted if not edge.counterfactual_check.strip()
    ]
    for edge in invalidator_violations:
        add("invalidator", f"invalidator:{edge.edge_id}")

    # 7) 无支撑边比例：没有证据的边只能是 hypothesis，不能撑起主因权重。
    unsupported_edges = [edge for edge in edges_sorted if not edge.supporting_evidence_ids]
    unsupported_ratio = _ratio(
        len(unsupported_edges), len(edges_sorted), empty_value=0.0
    )
    if unsupported_ratio > unsupported_edge_ratio_threshold:
        add(
            "edge_support",
            "edge_support:unsupported_ratio",
            materiality="high",
        )

    scores = SufficiencyScores(
        evidence_coverage=_ratio(covered, len(hypotheses_sorted), empty_value=0.0),
        time_order=_ratio(
            len(evidence_sorted) - len(time_violations), len(evidence_sorted), empty_value=1.0
        ),
        mechanism=_ratio(
            len(edges_sorted) - len(mechanism_violations), len(edges_sorted), empty_value=1.0
        ),
        magnitude=_ratio(
            len(edges_sorted) - len(magnitude_violations), len(edges_sorted), empty_value=1.0
        ),
        alternative=1.0 if alternative_ok else 0.0,
        invalidator=_ratio(
            len(edges_sorted) - len(invalidator_violations), len(edges_sorted), empty_value=1.0
        ),
    )

    return SufficiencyReport(
        run_id=run_id,
        issue_id=issue_id,
        as_of=as_of,
        data_cutoff=data_cutoff,
        passed=not gaps,
        gaps=sorted(gaps, key=lambda gap: (gap.gap_type, gap.gap_id)),
        scores=scores,
        unsupported_edge_ratio=unsupported_ratio,
    )
