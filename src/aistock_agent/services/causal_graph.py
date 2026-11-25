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


def _validate_structure(nodes: dict[str, CausalNode], edges: list[CausalEdge]) -> None:
    """机制图的结构自检。

    **这里只做结构检查，不做数据验证。** 通过本函数不代表边有效——
    边的有效性需要「宏观 gap 序列 vs 目标节点收益序列」的滞后相关性检验
    （见 spec 2026-11-12 §4）。当前数据不足，所有边仍是 prior_source=theory。

    为什么要查这三项：
      - lag_days 越界：宏观传导按月频给 3–10 天，超过 10 天说明标错了单位
      - 环：传导链成环会让多跳遍历无限展开或重复计权
      - prior_source 拼错：会让权重计算静默降级到默认分支，难以察觉
    """
    valid_prior = {"theory", "expert", "data_fit"}

    for edge in edges:
        if not 0 <= edge.lag_days <= 10:
            raise ValueError(
                f"edge lag_days out of range (0-10): {edge.source}->{edge.target} lag={edge.lag_days}"
            )
        if edge.prior_source not in valid_prior:
            raise ValueError(
                f"edge prior_source invalid: {edge.source}->{edge.target} got={edge.prior_source}"
            )

    adjacency: dict[str, list[str]] = {}
    for edge in edges:
        adjacency.setdefault(edge.source, []).append(edge.target)

    white, gray, black = 0, 1, 2
    color = {node_id: white for node_id in nodes}

    def visit(node_id: str, stack: list[str]) -> None:
        color[node_id] = gray
        stack.append(node_id)
        for next_id in adjacency.get(node_id, []):
            if color.get(next_id) == gray:
                cycle = stack[stack.index(next_id):] + [next_id]
                raise ValueError(f"causal graph has a cycle: {' -> '.join(cycle)}")
            if color.get(next_id) == white:
                visit(next_id, stack)
        stack.pop()
        color[node_id] = black

    for node_id in nodes:
        if color[node_id] == white:
            visit(node_id, [])


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
            kind=item["kind"],
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
                direction=int(item["direction"]),
                lag_days=int(item["lag_days"]),
                decay=float(item["decay"]),
                condition=item.get("condition"),
                prior_source=item["prior_source"],
                note=str(item.get("note", "")),
            )
        )

    _validate_structure(nodes, edges)
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
