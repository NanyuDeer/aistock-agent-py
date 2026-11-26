"""六因子链投影：只负责压缩已通过门禁的因果路径。

约束（设计规格 §4.1 / §6.2）：
- 六因子链只负责展示，不重新研究、不新增 claim；
- 只有 `gate_passed=True` 且有证据支撑的边才能标记为 `supported`；
- 无证据或 edge_type=hypothesis 的边一律标记为假设，并单独列出 edge ID；
- 投影只允许引用调用方提供的冻结证据集合，账本外证据不得进入展示。
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from typing import Literal

from pydantic import Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.research import CausalEdge, Hypothesis

SIX_FACTOR_STAGES: tuple[str, ...] = (
    "structural_root",
    "trigger",
    "transmission",
    "exposure",
    "repricing",
    "observable_result",
)

StepStatus = Literal["supported", "hypothesis"]


class FactorStep(V4Contract):
    """单个因子的展示投影；statement 只复制已有边的 mechanism。"""

    stage: str
    statement: str
    evidence_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)
    status: StepStatus
    confidence: float = Field(ge=0.0, le=1.0)


class SixFactorChain(V4Contract):
    """六因子链投影结果；`hypothesis_only_edge_ids` 显式暴露假设边。"""

    chain_id: str
    snapshot_id: str
    gate_passed: bool
    steps: list[FactorStep]
    hypothesis_only_edge_ids: list[str] = Field(default_factory=list)


def _best_edge_by_stage(causal_edges: Sequence[CausalEdge]) -> dict[str, CausalEdge]:
    best: dict[str, CausalEdge] = {}
    for edge in sorted(causal_edges, key=lambda item: item.edge_id):
        if edge.to_node not in SIX_FACTOR_STAGES:
            continue
        current = best.get(edge.to_node)
        if current is None or edge.confidence > current.confidence:
            best[edge.to_node] = edge
    return best


def project_six_factor(
    *,
    hypotheses: Sequence[Hypothesis],
    causal_edges: Sequence[CausalEdge],
    evidence_ids: Collection[str],
    snapshot_id: str,
    gate_passed: bool,
) -> SixFactorChain:
    """把已通过门禁的因果路径压缩成六因子链；不新增任何 claim。"""
    # 假设集合只用于确认调用方已冻结竞争解释；投影本身不写回假设。
    _ = hypotheses
    allowed_evidence = set(evidence_ids)
    best = _best_edge_by_stage(causal_edges)

    steps: list[FactorStep] = []
    for stage in SIX_FACTOR_STAGES:
        edge = best.get(stage)
        if edge is None:
            steps.append(
                FactorStep(
                    stage=stage,
                    statement=f"no_gated_path:{stage}",
                    status="hypothesis",
                    confidence=0.0,
                )
            )
            continue
        support = sorted(set(edge.supporting_evidence_ids) & allowed_evidence)
        supported = gate_passed and bool(support) and edge.edge_type != "hypothesis"
        steps.append(
            FactorStep(
                stage=stage,
                statement=edge.mechanism,
                evidence_ids=support,
                edge_ids=[edge.edge_id],
                status="supported" if supported else "hypothesis",
                confidence=edge.confidence,
            )
        )

    hypothesis_only = sorted(
        edge.edge_id
        for edge in causal_edges
        if not edge.supporting_evidence_ids or edge.edge_type == "hypothesis"
    )
    return SixFactorChain(
        chain_id=f"chain_{snapshot_id}",
        snapshot_id=snapshot_id,
        gate_passed=gate_passed,
        steps=steps,
        hypothesis_only_edge_ids=hypothesis_only,
    )
