"""Chat 子图拓扑测试：general_fallback 分支（P7+P8 线 1 Task 1）。"""
from langgraph.checkpoint.memory import MemorySaver

from aistock_agent.graph.chat_builder import build_chat_graph, route_after_router


def test_route_after_router_general_science() -> None:
    state = {"general_source": "science", "complexity": "light"}
    assert route_after_router(state) == "general_fallback"


def test_route_after_router_general_gap() -> None:
    state = {"general_source": "gap", "complexity": "light"}
    assert route_after_router(state) == "general_fallback"


def test_route_after_router_general_priority_over_deep() -> None:
    # general_source 优先于 deep escalate（科普/缺口绝不升级 deep）
    state = {"general_source": "gap", "complexity": "deep"}
    assert route_after_router(state) == "general_fallback"


def test_route_after_router_deep_still_escalates() -> None:
    state = {"complexity": "deep"}
    assert route_after_router(state) == "escalate"


def test_graph_has_general_fallback_node() -> None:
    graph = build_chat_graph()
    assert "general_fallback" in graph.nodes


def test_graph_compiles_with_general_fallback() -> None:
    compiled = build_chat_graph().compile(checkpointer=MemorySaver())
    assert compiled is not None
