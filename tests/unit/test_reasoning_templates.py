"""节点 reasoning prompt 模板单测。"""
from aistock_agent.prompts.chat.reasoning import REASONING_TEMPLATES, render_reasoning_prompt


def test_all_chat_nodes_have_template():
    expected = {"qa_router", "skill_executor", "synth_answer", "escalate"}
    assert expected.issubset(REASONING_TEMPLATES.keys())


def test_render_qa_router():
    prompt = render_reasoning_prompt(
        node="qa_router",
        question="查一下 600519 的行情",
        context={"symbols": ["600519"], "intent": "stock_snapshot"},
    )
    assert "查一下 600519 的行情" in prompt
    assert "拆解" in prompt or "理解" in prompt


def test_render_skill_executor_lists_skills():
    prompt = render_reasoning_prompt(
        node="skill_executor",
        question="查 600519",
        context={"skills": ["stock_snapshot"], "symbols": ["600519"]},
    )
    assert "stock_snapshot" in prompt
    assert "收集证据" in prompt


def test_render_unknown_node_raises():
    import pytest
    with pytest.raises(KeyError):
        render_reasoning_prompt(node="unknown", question="x", context={})
