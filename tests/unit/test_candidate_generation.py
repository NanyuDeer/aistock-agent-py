"""L2 候选生成单测（读真实机制图，不打网络）。"""

from __future__ import annotations

import pytest

from aistock_agent.services.candidate_generation import (
    Candidate,
    Trigger,
    generate_candidates,
)
from aistock_agent.services.causal_graph import load_market_graph


@pytest.fixture(scope="module")
def graph():  # noqa: ANN201
    return load_market_graph()


def _trig(node: str, strength: float = 0.8, *, kind: str = "catalyst") -> Trigger:
    return Trigger(
        kind=kind,  # type: ignore[arg-type]
        source_node=node,
        ref=f"test:{node}",
        title=f"测试触发 {node}",
        strength=strength,
    )


def test_fed_rate_produces_candidate_with_observable(graph) -> None:  # noqa: ANN001
    """FED_RATE → US10Y → 创业板，路径存在，应产出候选。"""
    out = generate_candidates(graph, [_trig("FED_RATE")])

    assert len(out) == 1
    cand = out[0]
    assert cand.source_node == "FED_RATE"
    assert "IDX_GEM" in cand.targets
    assert cand.strength > 0


def test_candidate_targets_only_observable_kinds(graph) -> None:  # noqa: ANN001
    """targets 里不能出现 factor / flow —— 那是路径上的中间节点，不是结果。"""
    out = generate_candidates(graph, [_trig("FED_RATE")])

    for target in out[0].targets:
        node = graph.node(target)
        assert node is not None
        assert node.kind in {"market", "sector", "theme"}, f"{target} 是 {node.kind}"


def test_node_without_path_produces_nothing(graph) -> None:  # noqa: ANN001
    """起点传不到任何可观测标的 → 不产出候选（三要素缺一不可）。"""
    # 用一个没有出边的节点：IDX_AVG_PRICE 在机制图里没有出边
    out = generate_candidates(graph, [_trig("IDX_AVG_PRICE")])

    assert out == []


def test_unknown_node_produces_nothing(graph) -> None:  # noqa: ANN001
    """图谱里不存在的节点不该崩，也不该造候选。"""
    out = generate_candidates(graph, [_trig("NOT_A_REAL_NODE")])

    assert out == []


def test_same_node_keeps_only_strongest_trigger(graph) -> None:  # noqa: ANN001
    """两个事件都指向同一节点时只留最强的那条——否则同一原因占两票。"""
    triggers = [
        _trig("FED_RATE", 0.3),
        _trig("FED_RATE", 0.9),
    ]

    out = generate_candidates(graph, triggers)

    assert len(out) == 1
    assert out[0].trigger.strength == 0.9


def test_candidates_sorted_by_strength(graph) -> None:  # noqa: ANN001
    out = generate_candidates(
        graph,
        [_trig("FED_RATE", 0.2), _trig("VIX", 0.9)],
    )

    strengths = [c.strength for c in out]
    assert strengths == sorted(strengths, reverse=True)


def test_max_candidates_respected(graph) -> None:  # noqa: ANN001
    triggers = [_trig(n) for n in ("FED_RATE", "VIX", "USDCNH", "DR007", "POLICY_RATE")]

    out = generate_candidates(graph, triggers, max_candidates=2)

    assert len(out) <= 2


def test_zero_max_returns_empty(graph) -> None:  # noqa: ANN001
    assert generate_candidates(graph, [_trig("FED_RATE")], max_candidates=0) == []


def test_observables_sorted_by_strength(graph) -> None:  # noqa: ANN001
    out = generate_candidates(graph, [_trig("FED_RATE")])

    cand = out[0]
    values = [cand.targets[t] for t in cand.observables]
    assert values == sorted(values, reverse=True)


def test_no_triggers_returns_empty(graph) -> None:  # noqa: ANN001
    assert generate_candidates(graph, []) == []


def test_candidate_is_frozen() -> None:
    """候选是不可变的——下游只读，避免投票阶段被就地改写。"""
    cand = Candidate(
        source_node="X",
        trigger=Trigger(kind="catalyst", source_node="X", ref="r", title="t", strength=1.0),
        targets={"IDX_CSI300": 0.5},
        strength=0.5,
    )
    with pytest.raises(Exception):
        cand.strength = 0.9  # type: ignore[misc]
