"""synth_answer 交互式确认短路单测（Phase 4-2，改进 13）。

覆盖：state.confirm 非空 → 短路返回 confirm（不渲染、不调 LLM）；
confirm 短路优先于 goal 缺失检查（防御，qa_router confirm 分支恒有 goal）。
"""
from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.synth_answer import synth_answer_node
from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.state.chat_schema import QuestionState

CONFIRM = {
    "question": "我想了解一下贵州茅台和五粮液",
    "options": [
        {"key": "600519", "label": "贵州茅台(600519)"},
        {"key": "000858", "label": "五粮液(000858)"},
        {"key": "none", "label": "都不是"},
    ],
}


def _state(**extra) -> QuestionState:
    state: QuestionState = {
        "messages": [HumanMessage(content="我想了解一下贵州茅台和五粮液")],
        "goal": InsightGoal(
            question="我想了解一下贵州茅台和五粮液", intent="stock_snapshot"
        ),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }
    state.update(extra)
    return state


@pytest.mark.asyncio
async def test_confirm_short_circuit_returns_confirm_without_rendering() -> None:
    """state.confirm 非空 → 短路返回 confirm（不渲染、不调 LLM）。"""
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("confirm 短路不应调用 LLM"),
    ):
        result = await synth_answer_node(_state(confirm=CONFIRM))
    assert result["final_response"] == ""
    assert result["confirm"] == CONFIRM


@pytest.mark.asyncio
async def test_confirm_short_circuit_before_goal_missing_check() -> None:
    """confirm 短路位于澄清/缺 goal 检查之前（防御：不渲染错误话术）。"""
    state = _state(confirm=CONFIRM)
    state["goal"] = None
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("confirm 短路不应调用 LLM"),
    ):
        result = await synth_answer_node(state)
    assert result["confirm"] == CONFIRM
    assert "内部错误" not in result["final_response"]
