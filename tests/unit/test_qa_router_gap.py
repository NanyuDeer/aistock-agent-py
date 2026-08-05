"""qa_router 缺口识别测试（P7+P8 线 1 Task 5，D37 仅确定性缺口）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.graph.nodes.qa_router import qa_router_node
from aistock_agent.state.chat_schema import QuestionState


def _state(message: str) -> QuestionState:
    from langchain_core.messages import HumanMessage

    return {"messages": [HumanMessage(content=message)], "force_deep": None}  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_capability_gap_routes_to_gap_mode() -> None:
    # 真实路径：LLM 故障 + 无关键词命中（route_by_keyword_fallback 返回默认
    # report_lookup，非 None）→ keyword_miss=True；resolve 失败后靠
    # _has_non_stock_intent（含"A股"）判为能力型缺口 → general_source="gap"。
    # 只 mock LLM 失败与名称解析网络调用，不 mock 关键词/候选/意图判定。
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=RuntimeError("llm down"),
    ), patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value=None),
    ):
        out = await qa_router_node(_state("美联储加息对A股有什么影响"))
    assert out.get("general_source") == "gap"
    assert out["skill_calls"] == []
    assert "clarification" not in out


@pytest.mark.asyncio
async def test_missing_symbol_still_clarifies() -> None:
    # 个股缺码澄清路径不变：名称解析失败 → clarification（不误判为缺口）
    with patch(
        "aistock_agent.graph.nodes.qa_router.route_by_keyword_fallback",
        return_value=AsyncMock(skill_name="stock_snapshot", args={}),
    ), patch(
        "aistock_agent.graph.nodes.qa_router._resolve_stock_from_message",
        AsyncMock(return_value=None),
    ), patch("aistock_agent.graph.nodes.qa_router.get_quick_think") as llm_mock:
        out = await qa_router_node(_state("xxx股票怎么样"))
    assert out.get("general_source") is None
    assert out.get("clarification")
    # LLM 被尝试一次（mock 抛 TypeError 进入兜底链）；缺口/澄清判定本身确定性（关键词+名称候选决定）
    llm_mock.assert_called_once()
