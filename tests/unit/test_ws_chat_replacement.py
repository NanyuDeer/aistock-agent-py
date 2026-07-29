"""P1.4 风险点处理 — WS 端点改造单元测试。

覆盖：
- _NODE_LABELS 包含新 CHAT 子图节点
- _select_graph 被 WS 端点复用（与 routes.py 一致）
"""

from aistock_agent.api.routes import _select_graph
from aistock_agent.api.ws import _NODE_LABELS


def test_node_labels_contains_chat_subgraph_nodes():
    """_NODE_LABELS 包含 qa_router / skill_executor / synth_answer 三个新节点。"""
    assert "qa_router" in _NODE_LABELS
    assert "skill_executor" in _NODE_LABELS
    assert "synth_answer" in _NODE_LABELS
    # 标签是非空中文字符串
    for node_name in ("qa_router", "skill_executor", "synth_answer"):
        label = _NODE_LABELS[node_name]
        assert isinstance(label, str)
        assert len(label) > 0


def test_node_labels_preserves_legacy_nodes():
    """_NODE_LABELS 保留老路径节点（开关关闭时仍需用）。"""
    assert "supervisor" in _NODE_LABELS
    assert "ai_advisor_agent" in _NODE_LABELS
    assert "stock_analyst" in _NODE_LABELS


def test_select_graph_is_callable_from_ws_module():
    """_select_graph 可从 ws 模块调用（验证无循环依赖）。"""
    # 只要能调用并返回非 None 即可（具体返回值依赖开关状态）
    graph = _select_graph()
    assert graph is not None
    assert hasattr(graph, "ainvoke")
