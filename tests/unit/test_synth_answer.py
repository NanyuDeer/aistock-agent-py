"""synth_answer 节点单元测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from aistock_agent.graph.nodes.synth_answer import SynthOutput, _build_prompt, synth_answer_node
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


def _state(message: str = "茅台现在多少钱") -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": InsightGoal(question=message, intent="stock_snapshot"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
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


def test_synth_output_wrapped_shape_validates() -> None:
    """包装形态 {"insight": {...}} 可通过 SynthOutput 校验。"""
    output = SynthOutput.model_validate(
        {
            "insight": {
                "conclusion": "结论",
                "basis": [],
                "confidence": "low",
                "uncertainty": [],
                "answer_mode": "validate",
            }
        }
    )
    assert output.insight.conclusion == "结论"
    assert output.insight.answer_mode == "validate"


def test_synth_output_rejects_bare_insight_shape() -> None:
    """裸 Insight 形态（顶层直接是 conclusion/basis 等字段）必须失败。"""
    with pytest.raises(ValidationError):
        SynthOutput.model_validate(
            {
                "conclusion": "裸字段",
                "basis": [],
                "confidence": "low",
                "uncertainty": [],
                "answer_mode": "validate",
            }
        )


def test_build_prompt_declares_top_level_insight_wrapper() -> None:
    """Prompt 包含 insight 顶层契约并禁止裸字段。"""
    goal = InsightGoal(question="茅台现在多少钱", intent="stock_snapshot")
    prompt = _build_prompt(goal, [], "validate")
    assert '"insight"' in prompt
    assert '"answer_mode"' in prompt
    assert "顶层只能有 insight" in prompt
    assert "禁止输出裸 conclusion" in prompt


@pytest.mark.asyncio
async def test_synth_answer_parse_error_still_degrades_safely() -> None:
    """真实解析异常（Pydantic ValidationError）→ 安全降级，不中断图执行。"""

    def _raise_parse_error(*args, **kwargs):
        # 模拟 json_mode 对非契约输出的真实校验失败
        SynthOutput.model_validate({})  # 缺 insight

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_raise_parse_error))
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state())

    assert result["final_response"].startswith("综合回答生成失败")
    assert result["insight"].confidence == "low"
    assert result["insight"].answer_mode == "validate"
    assert result["trace"].actual_mode == "validate"
