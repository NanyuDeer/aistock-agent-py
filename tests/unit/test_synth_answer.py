"""synth_answer 节点单元测试。"""
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.synth_answer import synth_answer_node
from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.state.chat_schema import QuestionState

CLARIFICATION = "请提供 6 位股票代码后重试。"


def _state_with_clarification(message: str = "茅台最近新闻") -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": InsightGoal(question=message, intent="stock_news"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "clarification": CLARIFICATION,
    }


@pytest.mark.asyncio
async def test_synth_answer_clarification_short_circuits() -> None:
    """澄清路径短路：不触发 deep LLM，返回低置信度澄清响应。"""
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("deep LLM should not be called on clarification path"),
    ):
        result = await synth_answer_node(_state_with_clarification())

    assert result["final_response"] == CLARIFICATION
    assert result["insight"].conclusion == CLARIFICATION
    assert result["insight"].confidence == "low"
    assert result["insight"].answer_mode == "validate"
    assert result["trace"].actual_mode == "validate"
    assert result["trace"].skill_calls == []
    assert result["messages"][0].content == CLARIFICATION
