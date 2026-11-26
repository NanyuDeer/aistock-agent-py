"""确定性因果影响遍历：只解锁被回改假设的下游节点。

回改不能“顺手”清空整张图。设计规格 §5.1 要求：保留未受影响的 evidence
和 claims，只重新计算受影响的后代。本模块因此只沿因果边向下遍历，绝不
反向触达上游父节点或挂在其他分支上的兄弟假设。

版本处理：因果图可能以 `hyp_x:v1` 建边，而回改后的当前假设是 `hyp_x:v2`。
为避免每次改版本都重建整张图，根节点匹配沿用 hypothesis id 的主干（`:`
之前的部分）；这只影响起点匹配，不扩大到无关分支。
"""

from collections.abc import Sequence

from pydantic import Field

from ..contracts import V4Contract
from ..contracts.research import CausalEdge


class ImpactResult(V4Contract):
    """一次局部影响分析的结构化结果，供回改决策与落账引用。

    只包含下游节点；`root_ref` 本身不放进受影响列表，避免把“被改的假设”
    误当作“被波及的假设”。
    """

    root_ref: str
    impacted_hypothesis_refs: list[str] = Field(default_factory=list)
    impacted_claim_ids: list[str] = Field(default_factory=list)
    edge_ids: list[str] = Field(default_factory=list)


def _stem(ref: str) -> str:
    """取 `id:v{n}` 的主干，用于跨版本匹配同一条假设。"""
    return ref.split(":", 1)[0]


def _is_hypothesis_node(node: str) -> bool:
    """假设节点以 `hyp_` 开头或带 `:v{n}` 版本后缀；其余视为 claim。"""
    return ":v" in node or _stem(node).startswith("hyp_")


def impacted_refs(graph: Sequence[CausalEdge], hypothesis_ref: str) -> ImpactResult:
    """沿因果边做下游广度优先遍历，返回受影响的 hypothesis / claim id。

    - 边按 `edge_id` 稳定排序，保证遍历顺序与输入顺序无关；
    - `visited` 同时充当环保护，A→B→A 的图不会死循环；
    - 兄弟分支因不可达而天然被排除，无需额外黑名单。
    """
    ordered_edges = sorted(graph, key=lambda edge: edge.edge_id)
    visited: set[str] = {hypothesis_ref}
    frontier: list[str] = [hypothesis_ref]
    impacted_nodes: set[str] = set()
    used_edge_ids: set[str] = set()

    while frontier:
        current = frontier.pop(0)
        for edge in ordered_edges:
            matches_current = edge.from_node == current or (
                _is_hypothesis_node(edge.from_node) and _stem(edge.from_node) == _stem(current)
            )
            if not matches_current:
                continue
            used_edge_ids.add(edge.edge_id)
            target = edge.to_node
            if target in visited:
                continue
            visited.add(target)
            impacted_nodes.add(target)
            frontier.append(target)

    hypothesis_refs = sorted(node for node in impacted_nodes if _is_hypothesis_node(node))
    claim_ids = sorted(node for node in impacted_nodes if not _is_hypothesis_node(node))
    return ImpactResult(
        root_ref=hypothesis_ref,
        impacted_hypothesis_refs=hypothesis_refs,
        impacted_claim_ids=claim_ids,
        edge_ids=sorted(used_edge_ids),
    )
