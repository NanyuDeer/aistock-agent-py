"""qa_router 纠错否定测试（P9，线 1 Task 7）。

用户拍板边界：强否定词 + 上一轮存在可替换标的才触发；
无历史 / 弱否定（"不太对"）不触发，交既有路由。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.graph.nodes.qa_router import qa_router_node
from aistock_agent.schemas.chat_contract import SkillCall
from aistock_agent.state.chat_schema import QuestionState


def _state(prev_user: str, message: str) -> QuestionState:
    from langchain_core.messages import AIMessage, HumanMessage

    return {
        "messages": [
            HumanMessage(content=prev_user),
            AIMessage(content="已回答：贵州茅台当前价..."),
            HumanMessage(content=message),
        ],
        "force_deep": None,
    }  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_negation_correction_swaps_symbol() -> None:
    # "不是茅台，是五粮液" → 上轮意图 stock_snapshot，新标的 000858
    with patch(
        "aistock_agent.graph.nodes.qa_router._apply_negation_correction",
        AsyncMock(
            return_value={
                "goal": {
                    "question": "不是茅台，是五粮液",
                    "intent": "stock_snapshot",
                    "symbols": ["000858"],
                },
                "plan": "direct",
                "skill_calls": [
                    SkillCall(skill_name="stock_snapshot", args={"symbol": "000858"})
                ],
                "complexity": "light",
            }
        ),
    ) as corr_mock, patch("aistock_agent.graph.nodes.qa_router.get_quick_think") as llm_mock:
        out = await qa_router_node(_state("茅台怎么样", "不是茅台，是五粮液"))
    corr_mock.assert_awaited_once()
    # SkillCall 是 Pydantic 模型，属性访问（非下标）
    assert out["skill_calls"][0].args["symbol"] == "000858"
    llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_no_history_does_not_correct() -> None:
    from langchain_core.messages import HumanMessage

    state: QuestionState = {"messages": [HumanMessage(content="不是茅台，是五粮液")]}  # type: ignore[typeddict-item]
    with patch(
        "aistock_agent.graph.nodes.qa_router._apply_negation_correction",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value=None),
    ), patch("aistock_agent.graph.nodes.qa_router.get_quick_think"):
        out = await qa_router_node(state)
    assert out.get("general_source") is None  # 交既有路由，不特殊处理
