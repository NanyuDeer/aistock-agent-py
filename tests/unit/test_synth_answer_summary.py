"""synth_answer 长会话摘要注入单测（Phase 5 Task 1）。

覆盖（对齐 brief §测试要求 ⑥⑧）：
- ⑥ state.messages_summary 存在 → LLM prompt 注入"此前对话摘要"
- ⑥b messages_summary 缺失但消息超窗 → 本地 trim_messages 重算注入（幂等等价）
- ⑧ 短会话 → prompt 无摘要段（字节不变）
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.graph.nodes.synth_answer import SynthOutput, synth_answer_node
from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.state.chat_schema import QuestionState

MSG_QUOTE = "600519 现在多少钱"


def _history(n_turns: int) -> list:
    out = []
    for i in range(n_turns):
        out.append(HumanMessage(content=f"早期问题{i}"))
        out.append(AIMessage(content=f"早期回答{i}"))
    return out


def _state(n_hist_turns: int, summary: str | None = None) -> QuestionState:
    st: QuestionState = {
        "messages": _history(n_hist_turns) + [HumanMessage(content=MSG_QUOTE)],
        "goal": InsightGoal(question=MSG_QUOTE, intent="stock_snapshot"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }
    if summary is not None:
        st["messages_summary"] = summary
    return st


def _capturing_synth_llm(captured: dict) -> MagicMock:
    async def _ainvoke(messages: list) -> SynthOutput:
        captured["prompt"] = messages[0].content
        return SynthOutput.model_validate(
            {
                "insight": {
                    "conclusion": "贵州茅台今日震荡上行。",
                    "basis_indices": [],
                    "confidence": "low",
                    "answer_mode": "validate",
                }
            }
        )

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_ainvoke))
    )
    return mock_llm


# ── ⑥ state.messages_summary 存在 → 注入 ──


@pytest.mark.asyncio
async def test_synth_answer_injects_state_summary() -> None:
    captured: dict = {}
    mock_llm = _capturing_synth_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        await synth_answer_node(_state(0, summary="用户：早期问题0"))
    assert "此前对话摘要" in captured["prompt"]
    assert "用户：早期问题0" in captured["prompt"]


# ── ⑥b messages_summary 缺失 + 超窗 → 本地重算注入（幂等等价）──


@pytest.mark.asyncio
async def test_synth_answer_recomputes_summary_from_messages() -> None:
    captured: dict = {}
    mock_llm = _capturing_synth_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        await synth_answer_node(_state(7))  # 15 条，超窗 3 条，未带 messages_summary
    assert "此前对话摘要" in captured["prompt"]
    assert "用户：早期问题0" in captured["prompt"]


# ── ⑧ 短会话：prompt 无摘要段（字节不变）──


@pytest.mark.asyncio
async def test_synth_answer_short_session_prompt_unchanged() -> None:
    captured: dict = {}
    mock_llm = _capturing_synth_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        await synth_answer_node(_state(5))  # 11 条
    assert "此前对话摘要" not in captured["prompt"]
