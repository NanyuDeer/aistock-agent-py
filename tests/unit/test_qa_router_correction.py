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


# ── 以下为 T7 review 修复后新增的真实路径测试（不 mock _apply_negation_correction）──


@pytest.mark.asyncio
async def test_real_negation_swap_name_symbol() -> None:
    # 旗舰示例："不是茅台，是五粮液" → 名称路径取句末"五粮液" → resolve 000858
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(side_effect=lambda name: "000858" if name == "五粮液" else None),
    ), patch("aistock_agent.graph.nodes.qa_router.get_quick_think") as llm_mock:
        out = await qa_router_node(_state("茅台怎么样", "不是茅台，是五粮液"))
    # 纠错短路：无 general 信号、skill 直达新标的、goal 标记纠错
    assert out.get("general_source") is None
    assert out["skill_calls"][0].args["symbol"] == "000858"
    assert out["goal"].symbols == ["000858"]
    assert out["goal"].constraints.get("negation_correction") == "true"
    llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_real_negation_single_code_no_resolve() -> None:
    # 显式单个 6 位代码直接作新标的（FIX①a），不调 resolve_symbol
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(),
    ) as resolve_mock, patch("aistock_agent.graph.nodes.qa_router.get_quick_think") as llm_mock:
        out = await qa_router_node(_state("茅台怎么样", "不是茅台，是000858"))
    assert out["skill_calls"][0].args["symbol"] == "000858"
    resolve_mock.assert_not_awaited()
    llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_real_negation_no_history_no_correction() -> None:
    # 无历史（仅一轮）→ 纠错短路被绕过（FIX②），走既有路由（个股名解析失败 → 澄清）
    from langchain_core.messages import HumanMessage

    state: QuestionState = {"messages": [HumanMessage(content="错了，看五粮液")]}  # type: ignore[typeddict-item]
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value=None),
    ), patch("aistock_agent.graph.nodes.qa_router.get_quick_think") as llm_mock:
        out = await qa_router_node(state)
    assert out.get("general_source") is None
    assert out["skill_calls"] == []
    # 未触发纠错：goal 无 negation_correction 约束
    assert out["goal"].constraints.get("negation_correction") is None
    llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_real_negation_index_correction_routes_index_snapshot() -> None:
    # 指数纠错（spec §2.5）："我说的是深成指" → 对齐闸门 1 消歧 →
    # index_snapshot(symbols=["399001"])，不再构造个股 skill（stock_snapshot(symbol=指数名)）。
    from langchain_core.messages import AIMessage, HumanMessage

    state: QuestionState = {
        "messages": [
            HumanMessage(content="深成指怎么样"),
            AIMessage(content="已回答：深成指..."),
            HumanMessage(content="我说的是深成指"),
        ],
        "force_deep": None,
    }  # type: ignore[typeddict-item]
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(),
    ) as resolve_mock, patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think"
    ) as llm_mock:
        out = await qa_router_node(state)
    assert out["skill_calls"][0].skill_name == "index_snapshot"
    assert out["skill_calls"][0].args["symbols"] == ["399001"]
    assert out["goal"].intent == "index_snapshot"
    assert out["goal"].constraints.get("negation_correction") == "true"
    # 指数分支确定性构造，不调名称解析、不进 LLM
    resolve_mock.assert_not_awaited()
    llm_mock.assert_not_called()
