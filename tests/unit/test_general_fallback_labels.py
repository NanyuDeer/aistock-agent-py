"""general_fallback 节点标签/reasoning 模板存在性测试（P7+P8 线 1 Task 6）。"""


def test_ws_node_labels_has_general_fallback() -> None:
    from aistock_agent.api.ws import _NODE_LABELS

    assert "general_fallback" in _NODE_LABELS


def test_reasoning_fallback_labels_has_general_fallback() -> None:
    from aistock_agent.graph.nodes._reasoning import _FALLBACK_LABELS

    assert "general_fallback" in _FALLBACK_LABELS


def test_reasoning_templates_has_general_fallback() -> None:
    from aistock_agent.prompts.chat.reasoning import REASONING_TEMPLATES

    assert "general_fallback" in REASONING_TEMPLATES
