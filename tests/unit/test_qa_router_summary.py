"""qa_router 长会话窗口 + 摘要注入单测（Phase 5 Task 1）。

覆盖（对齐 brief §测试要求 ⑤⑦⑧）：
- ⑤ 超窗：LLM prompt 注入"此前对话摘要"（含超窗用户问句）+ LLM messages 用 window（12 条）
- ⑦ summary 持久化：qa_router 返回 dict 含 messages_summary（随 checkpointer 持久化）
- ⑧ 短会话（≤12）：prompt 无摘要段（字节不变）+ 返回 dict 不含 messages_summary 键
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.graph.nodes.qa_router import QARouterOutput, qa_router_node
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.utils.context_window import trim_messages

MSG_QUOTE = "600519 现在多少钱"


def _history(n_turns: int) -> list:
    out = []
    for i in range(n_turns):
        out.append(HumanMessage(content=f"早期问题{i}"))
        out.append(AIMessage(content=f"早期回答{i}"))
    return out


def _state(n_hist_turns: int) -> QuestionState:
    """n_hist_turns 轮历史 + 当前问句（总消息数 = n_hist_turns*2 + 1）。"""
    return {
        "messages": _history(n_hist_turns) + [HumanMessage(content=MSG_QUOTE)],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "complexity": None,
        "force_deep": None,
        "user_id": "u_42",
    }


def _capturing_llm(captured: dict) -> MagicMock:
    fake_output = QARouterOutput(
        goal=InsightGoal(question=MSG_QUOTE, intent="stock_snapshot", symbols=["600519"]),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
        complexity="light",
    )

    async def _ainvoke(messages: list) -> QARouterOutput:
        captured["llm_messages"] = list(messages)
        captured["prompt"] = messages[0].content
        return fake_output

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_ainvoke))
    )
    return mock_llm


# ── ⑤ 超窗：prompt 注入摘要 + LLM 只见窗口 12 条 ──


@pytest.mark.asyncio
async def test_qa_router_over_window_injects_summary_and_window() -> None:
    """13 条（超窗 1 条）→ prompt 含"此前对话摘要"与超窗用户问句；LLM 消息 = 1 prompt + 12 窗口。"""
    captured: dict = {}
    mock_llm = _capturing_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ):
        result = await qa_router_node(_state(6))
    assert result["plan"] == "direct"
    assert "此前对话摘要" in captured["prompt"]
    assert "用户：早期问题0" in captured["prompt"]
    # LLM messages = [prompt] + window(12 条)
    assert len(captured["llm_messages"]) == 13
    assert len(captured["llm_messages"][1:]) == 12
    assert captured["llm_messages"][-1].content == MSG_QUOTE


# ── ⑦ summary 持久化：返回 dict 含 messages_summary（随 checkpointer 持久化）──


@pytest.mark.asyncio
async def test_qa_router_writes_messages_summary_over_window() -> None:
    """17 条超窗 → 返回 dict 写 messages_summary，且与 trim_messages 重算结果一致（幂等）。"""
    captured: dict = {}
    mock_llm = _capturing_llm(captured)
    st = _state(8)  # 17 条，超窗 5 条
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ):
        result = await qa_router_node(st)
    expected = trim_messages(st["messages"])[1]
    assert expected is not None
    assert result["messages_summary"] == expected


# ── ⑧ 短会话（≤12）：prompt 字节不变 + 不写 messages_summary ──


@pytest.mark.asyncio
async def test_qa_router_short_session_prompt_unchanged() -> None:
    """11 条 → prompt 无摘要段（字节不变），返回 dict 无 messages_summary 键，LLM 见全量消息。"""
    captured: dict = {}
    mock_llm = _capturing_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ):
        result = await qa_router_node(_state(5))  # 5*2+1 = 11 条
    assert "此前对话摘要" not in captured["prompt"]
    assert "messages_summary" not in result
    assert len(captured["llm_messages"][1:]) == 11
