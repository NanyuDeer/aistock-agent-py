"""机制图（市场版）加载与多跳查询。"""

from __future__ import annotations

import json

import pytest

from aistock_agent.services.causal_graph import (
    PRIOR_WEIGHT,
    _parse,
    load_market_graph,
)


@pytest.fixture(scope="module")
def graph():  # noqa: ANN201
    return load_market_graph()


def test_market_graph_has_expected_shape(graph) -> None:  # noqa: ANN001
    assert len(graph.nodes) == 23
    assert len(graph.edges) == 18
    kinds = {node.kind for node in graph.nodes.values()}
    assert kinds == {"factor", "market", "sector", "theme", "flow"}


def test_2026_11_05_overseas_semiconductor_path_exists(graph) -> None:  # noqa: ANN001
    """11-05 的跨市场路径：费城半导体 → A 股半导体。"""
    assert graph.path_exists("IDX_SOX", "SEC_SEMI")


def test_two_hop_reaches_downstream_and_one_hop_does_not(graph) -> None:  # noqa: ANN001
    """存储 → 半导体 → 光模块 是两跳；限制一跳时不应到达光模块。"""
    assert graph.path_exists("SEC_MEMORY", "SEC_OPTICAL", max_hops=2) is True
    assert graph.path_exists("SEC_MEMORY", "SEC_OPTICAL", max_hops=1) is False


def test_propagation_strength_decays_per_hop(graph) -> None:  # noqa: ANN001
    """每跳乘 decay：存储→半导体 0.8，半导体→光模块 0.7，累计 0.56。"""
    reachable = graph.reachable("SEC_MEMORY", max_hops=2)
    assert reachable["SEC_SEMI"] == 0.8
    assert reachable["SEC_OPTICAL"] == 0.56


def test_conditional_edge_keeps_condition(graph) -> None:  # noqa: ANN001
    """国产替代对冲外部利空是条件边——没有它，11-05 会被误判为「传导失效」。"""
    conditional = [edge for edge in graph.edges if edge.condition]
    assert len(conditional) == 1
    edge = conditional[0]
    assert edge.source == "THEME_DOMESTIC_SUB"
    assert edge.target == "SEC_SEMI"
    assert "外部半导体下跌" in (edge.condition or "")


def test_prior_weight_orders_data_above_theory() -> None:
    """理论边不能单独支撑结论，权重必须低于数据拟合边。"""
    assert PRIOR_WEIGHT["data_fit"] > PRIOR_WEIGHT["expert"] > PRIOR_WEIGHT["theory"]


def test_dangling_edge_is_rejected(tmp_path) -> None:  # noqa: ANN001
    """悬挂边会让多跳遍历静默丢路径，必须在加载时失败。"""
    bad = {
        "nodes": [{"id": "A", "kind": "factor", "name": "A"}],
        "edges": [
            {
                "source": "A",
                "target": "NOT_EXIST",
                "direction": 1,
                "lag_days": 0,
                "decay": 0.7,
                "condition": None,
                "prior_source": "theory",
            }
        ],
    }
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown node"):
        _parse(json.loads(path.read_text(encoding="utf-8")))
