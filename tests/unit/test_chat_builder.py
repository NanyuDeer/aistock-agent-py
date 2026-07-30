"""CHAT 子图构建测试。"""
from aistock_agent.graph.chat_builder import compile_chat_graph


def test_compile_chat_graph_returns_runnable():
    """图可正常编译，返回可调用对象。"""
    graph = compile_chat_graph(checkpointer=None)
    assert graph is not None
    assert hasattr(graph, "ainvoke")
    assert hasattr(graph, "astream_events")


def test_compile_chat_graph_idempotent():
    """多次编译返回独立实例。"""
    g1 = compile_chat_graph(checkpointer=None)
    g2 = compile_chat_graph(checkpointer=None)
    assert g1 is not g2
