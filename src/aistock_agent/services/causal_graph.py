"""机制图（先验因果图）—— 市场版最小集的加载与查询。

与 `IndustryKG` 的区别：
    `IndustryKG` 的节点是**行业**，边是产业上下游，回答「谁和谁同属一条链」。
    本模块的节点含**因子**（利率、VIX、DR007），边是因果传导（带方向/时滞/衰减），
    回答「谁会推涨或压跌谁、多久传导到位」。

    两者节点类型不同，故独立建图，不叠加。

数据源：`data/causal_edges_market.json`（人工策展，市场版 18 条）。

注意：本模块只负责「图长什么样、能查到什么」。**归因投票与 status 判定不在这里**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

PriorSource = Literal["theory", "expert", "data_fit"]
NodeKind = Literal["factor", "market", "sector", "theme", "flow"]

#: `prior_source` 在投票中的权重。理论边不能单独支撑结论。
PRIOR_WEIGHT: dict[PriorSource, float] = {
    "data_fit": 1.0,
    "expert": 0.7,
    "theory": 0.4,
}

#: 多跳遍历的默认停止阈值：累计强度低于此值即停。
MIN_PROPAGATION_STRENGTH = 0.15

#: 默认最大跳数。A 股产业链传导基本在上游材料 → 中游制造 → 下游应用内完成，
#: 第三跳起噪音大于信号。
MAX_HOPS = 2

_DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "causal_edges_market.json"


@dataclass(frozen=True)
class CausalNode:
    id: str
    kind: NodeKind
    name: str


@dataclass(frozen=True)
class CausalEdge:
    source: str
    target: str
    direction: int
    lag_days: int
    decay: float
    condition: str | None
    prior_source: PriorSource
    note: str = ""

    @property
    def weight(self) -> float:
        """该边在投票中的可信度权重。"""
        return PRIOR_WEIGHT[self.prior_source]


class CausalGraph:
    """机制图。构造后只读。"""

    def __init__(self, nodes: dict[str, CausalNode], edges: list[CausalEdge]) -> None:
        self._nodes = nodes
        self._edges = edges
        self._out: dict[str, list[CausalEdge]] = {}
        for edge in edges:
            self._out.setdefault(edge.source, []).append(edge)

    @property
    def nodes(self) -> dict[str, CausalNode]:
        return dict(self._nodes)

    @property
    def edges(self) -> list[CausalEdge]:
        return list(self._edges)

    def node(self, node_id: str) -> CausalNode | None:
        return self._nodes.get(node_id)

    def outgoing(self, node_id: str) -> list[CausalEdge]:
        return list(self._out.get(node_id, ()))

    def reachable(
        self,
        start: str,
        *,
        max_hops: int = MAX_HOPS,
        min_strength: float = MIN_PROPAGATION_STRENGTH,
    ) -> dict[str, float]:
        """从 `start` 出发的多跳传导，返回 {目标节点: 累计强度}。

        规则：
        - 每跳强度乘以该边的 `decay`
        - 累计强度低于 `min_strength` 的分支停止扩展
        - 超过 `max_hops` 停止
        - 同一节点取最强路径（不做多路径求和，避免环状放大）
        """
        strength: dict[str, float] = {}
        frontier: list[tuple[str, float, int]] = [(start, 1.0, 0)]

        while frontier:
            current, current_strength, hops = frontier.pop(0)
            if hops >= max_hops:
                continue
            for edge in self._out.get(current, ()):
                next_strength = current_strength * edge.decay
                if next_strength < min_strength:
                    continue
                if next_strength > strength.get(edge.target, 0.0):
                    strength[edge.target] = round(next_strength, 4)
                    frontier.append((edge.target, next_strength, hops + 1))

        return strength

    def path_exists(self, source: str, target: str, *, max_hops: int = MAX_HOPS) -> bool:
        """两节点之间是否存在传导路径（含条件边）。用于 M0 的覆盖度校验。"""
        if source == target:
            return True
        return target in self.reachable(source, max_hops=max_hops)


def _parse(data: dict[str, object]) -> CausalGraph:
    raw_nodes = data.get("nodes")
    raw_edges = data.get("edges")
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ValueError("causal graph json must contain list fields: nodes, edges")

    nodes: dict[str, CausalNode] = {}
    for item in raw_nodes:
        if not isinstance(item, dict):
            raise ValueError("causal node must be an object")
        node = CausalNode(
            id=str(item["id"]),
            kind=item["kind"],  # type: ignore[arg-type]
            name=str(item["name"]),
        )
        nodes[node.id] = node

    edges: list[CausalEdge] = []
    for item in raw_edges:
        if not isinstance(item, dict):
            raise ValueError("causal edge must be an object")
        source = str(item["source"])
        target = str(item["target"])
        # 悬挂边会让多跳遍历静默丢路径，宁可加载时直接失败
        for endpoint in (source, target):
            if endpoint not in nodes:
                raise ValueError(f"edge references unknown node: {endpoint}")
        edges.append(
            CausalEdge(
                source=source,
                target=target,
                direction=int(item["direction"]),  # type: ignore[arg-type]
                lag_days=int(item["lag_days"]),  # type: ignore[arg-type]
                decay=float(item["decay"]),  # type: ignore[arg-type]
                condition=item.get("condition"),  # type: ignore[arg-type]
                prior_source=item["prior_source"],  # type: ignore[arg-type]
                note=str(item.get("note", "")),
            )
        )

    return CausalGraph(nodes, edges)


@lru_cache(maxsize=1)
def load_market_graph(path: str | None = None) -> CausalGraph:
    """加载市场版机制图（带缓存）。"""
    target = Path(path) if path else _DATA_PATH
    with target.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError("causal graph json must be an object")
    return _parse(data)
