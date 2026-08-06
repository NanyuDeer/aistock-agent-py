"""qa_router 科普闸门升级测试（P7+P8 线 1 Task 4）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.graph.nodes.qa_router import qa_router_node
from aistock_agent.state.chat_schema import QuestionState


def _state(message: str, history: list[str] | None = None) -> QuestionState:
    from langchain_core.messages import AIMessage, HumanMessage

    msgs: list[object] = []
    for h in history or []:
        msgs.append(HumanMessage(content=h))
        msgs.append(AIMessage(content="已回答"))
    msgs.append(HumanMessage(content=message))
    return {"messages": msgs, "force_deep": None}  # type: ignore[typeddict-item]


@pytest.mark.asyncio
async def test_education_gate_sets_science_source() -> None:
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think") as llm_mock:
        out = await qa_router_node(_state("什么是市盈率"))
    assert out["general_source"] == "science"
    assert out["skill_calls"] == []
    assert out["complexity"] == "light"
    assert "final_response" not in out  # 不再固定话术短路
    llm_mock.assert_not_called()  # 科普零 LLM 路由（动态回答发生在 general 节点）


@pytest.mark.asyncio
async def test_education_variants_trigger_science() -> None:
    for msg in ("啥是K线", "怎么算换手率", "解释一下什么是涨停", "科普一下龙头股"):
        with patch("aistock_agent.graph.nodes.qa_router.get_quick_think") as llm_mock:
            out = await qa_router_node(_state(msg))
        assert out.get("general_source") == "science", f"{msg} 未触发科普"
        llm_mock.assert_not_called()


@pytest.mark.asyncio
async def test_non_education_not_hijacked() -> None:
    # "什么是今日主线" 是 compose 意图，不能被科普劫持
    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.build_compose_plan",
            return_value=[AsyncMock()],
        ),
        # D2：该问句会穿过闸门 2 触发真实 resolve_symbol（httpx → NodeApiClient），
        # 单测必须隔离网络，按仓库惯例 patch 为 None（resolve 未命中自然回落）
        patch(
            "aistock_agent.graph.nodes.qa_router.resolve_symbol",
            AsyncMock(return_value=None),
        ),
    ):
        out = await qa_router_node(_state("什么是今日主线"))
    assert out.get("general_source") is None
    assert out["plan"] == "compose"
