"""L2 候选生成 —— 从触发源产出 3~5 个归因候选。

spec: `docs/superpowers/specs/2026-11-09-market-causal-engine-design.md` §4.2 B2

## 三要素缺一不可（硬约束）

每个候选必须同时绑定：

1. **一个触发源** —— 预期差（actual − forecast）或催化事件
2. **一条图谱路径** —— 从触发源到可观测标的的传导链
3. **一个可观测结果** —— 价格/资金上能验证的落点（指数、板块）

缺任一项的触发源**直接丢弃**，不产出「弱候选」。

为什么这么严：spec §4.3 的核心主张是「不靠 LLM 联想、靠图谱传导」。
若允许「有事件但没路径」的候选存在，图谱就沦为装饰，价格 + 文本就能出
结论，等于绕回旧路（11-04 把创业板 +6.4% 归给下调再贷款利率，就是这个毛病）。

## 为什么候选是「少而粗」而不是「多而细」

M1 的图谱只有 29 条边。在这个密度下：

- 声称能区分 10 个候选的强弱，是自欺——大部分路径根本不存在
- 3~5 个候选、每个都有明确路径，至少**可被下游三族投票证伪**

宁可少，不可虚。图谱覆盖不到就是覆盖不到（spec §4.3 的「代价」段落）。

## 预期差缺失时的行为

`macro_expectations` 的 `forecast` 字段目前中国大陆指标基本为空（实测
forecast+actual 同时有值的记录为 0）。因此绝大多数宏观触发源会走
`Trigger.kind="catalyst"` 分支——靠事件本身，而不是预期差。

这不是降级失败，是当前数据条件下唯一诚实的做法。等预期值数据补齐
（`macro_shock.compute_macro_shock` 已经能吃 gap），同一套代码自动切回
`expectation_gap` 分支，**结构不用改**。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from aistock_agent.services.causal_graph import CausalGraph

#: 触发源类型。`expectation_gap` 需要 forecast，`catalyst` 只需要事件本身。
TriggerKind = Literal["expectation_gap", "catalyst"]

#: 可观测结果 = 能被行情/资金直接验证的落点。
#: 不含 factor（因子本身不可观测）、不含中间传导节点（那是路径不是结果）。
OBSERVABLE_KINDS: frozenset[str] = frozenset({"market", "sector", "theme"})


@dataclass(frozen=True)
class Trigger:
    """一个触发源。"""

    kind: TriggerKind
    source_node: str
    """图谱起点节点 ID（factor / market 等）。"""

    ref: str
    """可追溯的引用：macro_shock 用指标名，事件用 event_id。"""

    title: str
    """人读的说明。事件用标题，宏观用「CPI 超预期 0.2pp」。"""

    strength: float
    """0~1。预期差取 ``MacroShock.strength``；催化事件按影响分级。"""


@dataclass(frozen=True)
class Candidate:
    """一个归因候选。"""

    source_node: str
    trigger: Trigger
    targets: dict[str, float]
    """可观测目标 → 传导强度。空字典的候选不会存在（构造时已丢弃）。"""

    strength: float
    """综合强度 = trigger.strength × 最强目标强度。"""

    @property
    def observables(self) -> tuple[str, ...]:
        """按强度降序的可观测目标，供下游逐项检验。"""
        return tuple(t for t, _ in sorted(self.targets.items(), key=lambda kv: -kv[1]))


def _observable_targets(
    graph: CausalGraph, source: str, min_strength: float
) -> dict[str, float]:
    """沿图谱传导，只留下可观测落点。"""
    reached = graph.reachable(source, min_strength=min_strength)
    out: dict[str, float] = {}
    for node_id, strength in reached.items():
        node = graph.node(node_id)
        if node is None:
            continue
        if node.kind in OBSERVABLE_KINDS:
            out[node_id] = strength
    return out


def generate_candidates(
    graph: CausalGraph,
    triggers: Sequence[Trigger],
    *,
    max_candidates: int = 5,
    min_strength: float = 0.1,
) -> list[Candidate]:
    """把触发源转成候选列表，按强度降序，最多 ``max_candidates`` 个。

    Args:
        graph: 机制图（市场版）。
        triggers: 触发源。同一节点可多次出现（不同事件指向同一节点），
            取强度最高者——重复计数会放大同一原因。
        max_candidates: 上限。spec 要求 3~5 个，默认 5。
        min_strength: 传导强度下限，低于此值的路径视为「传不到」。

    Returns:
        候选列表。**无路径的触发源不产出候选**，所以长度可能小于
        ``len(triggers)``，也可能为 0——后者代表「今天找不到有路径的解释」，
        由上层判 ``insufficient``，不是异常。
    """
    if max_candidates <= 0:
        return []

    # 同一节点只留最强触发源：两个事件都指向 GLOBAL_RATE 时，
    # 生成两个候选会让同一个原因在投票里占两票。
    best_by_node: dict[str, Trigger] = {}
    for trig in triggers:
        current = best_by_node.get(trig.source_node)
        if current is None or trig.strength > current.strength:
            best_by_node[trig.source_node] = trig

    candidates: list[Candidate] = []
    for source, trig in best_by_node.items():
        targets = _observable_targets(graph, source, min_strength)
        if not targets:
            # 三要素缺「可观测结果」——丢弃，不产出弱候选。
            continue
        top = max(targets.values())
        candidates.append(
            Candidate(
                source_node=source,
                trigger=trig,
                targets=targets,
                strength=round(trig.strength * top, 4),
            )
        )

    candidates.sort(key=lambda c: c.strength, reverse=True)
    return candidates[:max_candidates]
