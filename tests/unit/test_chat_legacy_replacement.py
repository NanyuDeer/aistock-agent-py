"""P1.4 老路径替换 — 单元测试。

覆盖：
- build_chat_initial_state 构造的 QuestionState 字段
- settings.chat_graph_enabled 默认 False
- CHAT_NODE_LABELS 包含三个 CHAT 子图节点
"""

from aistock_agent.api.deps import build_chat_initial_state
from aistock_agent.config import settings
from aistock_agent.constants import CHAT_NODE_LABELS


def test_chat_graph_enabled_defaults_false():
    """开关默认 False，走老路径。"""
    assert settings.chat_graph_enabled is False


def test_build_chat_initial_state_fields():
    """build_chat_initial_state 返回的 QuestionState 字段与 /qa 端点对齐。"""
    state = build_chat_initial_state("茅台今天怎么样")

    assert state["goal"] is None
    assert state["plan"] == "direct"
    assert state["skill_calls"] == []
    assert state["evidences"] == []
    assert state["insight"] is None
    assert state["final_response"] == ""
    assert state["trace"] is None
    # messages 包含一条 HumanMessage
    assert len(state["messages"]) == 1
    assert state["messages"][0].content == "茅台今天怎么样"


def test_chat_node_labels_contains_three_nodes():
    """CHAT_NODE_LABELS 包含 qa_router / skill_executor / synth_answer 三个节点。"""
    assert "qa_router" in CHAT_NODE_LABELS
    assert "skill_executor" in CHAT_NODE_LABELS
    assert "synth_answer" in CHAT_NODE_LABELS
    # 标签是非空中文字符串
    for node_name in ("qa_router", "skill_executor", "synth_answer"):
        label = CHAT_NODE_LABELS[node_name]
        assert isinstance(label, str)
        assert len(label) > 0
