"""synth_answer 长会话摘要注入单测（Phase 5 Task 1 + review 修复）。

覆盖（对齐 brief §测试要求 ⑥⑧ + review finding）：
- ⑥ 超窗消息 → 单意图 LLM prompt 注入"此前对话摘要"（从当前 messages 重算）
- ⑥b 不读 state.messages_summary：跨轮残留的陈旧摘要不注入（Minor #1 回归）
- ⑥c 多子目标（goals 非空）路径每节 deep_think prompt 同样注入摘要（review 修复）
- ⑧ 短会话 → prompt 无摘要段（单意图 / 多子目标均字节不变）
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.graph.nodes.synth_answer import SynthOutput, synth_answer_node
from aistock_agent.schemas.chat_contract import InsightGoal, SubGoal
from aistock_agent.state.chat_schema import QuestionState

MSG_QUOTE = "600519 现在多少钱"


def _history(n_turns: int) -> list:
    out = []
    for i in range(n_turns):
        out.append(HumanMessage(content=f"早期问题{i}"))
        out.append(AIMessage(content=f"早期回答{i}"))
    return out


def _subgoal(question: str = "大盘当前表现") -> SubGoal:
    return SubGoal(
        id="g1",
        question=question,
        intent="market_snapshot",  # type: ignore[arg-type]
        dimension="validate",  # type: ignore[arg-type]
    )


def _state(
    n_hist_turns: int,
    summary: str | None = None,
    goals: list[SubGoal] | None = None,
) -> QuestionState:
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
    if goals is not None:
        st["goals"] = goals
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


# ── ⑥ 超窗消息 → 单意图路径注入（从当前 messages 重算）──


@pytest.mark.asyncio
async def test_synth_answer_injects_summary_over_window() -> None:
    captured: dict = {}
    mock_llm = _capturing_synth_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        await synth_answer_node(_state(7))  # 15 条，超窗 3 条
    assert "此前对话摘要" in captured["prompt"]
    assert "用户：早期问题0" in captured["prompt"]


# ── ⑥b 不读 state.messages_summary：陈旧摘要不注入（跨轮残留回归）──


@pytest.mark.asyncio
async def test_synth_answer_ignores_stale_messages_summary() -> None:
    """confirm 重跑把 messages 重置为 [] 而 checkpointer 保留上一轮摘要 → 不注入。"""
    captured: dict = {}
    mock_llm = _capturing_synth_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        # 短会话 + 残留 messages_summary（旧实现会注入陈旧摘要）
        await synth_answer_node(_state(0, summary="用户：陈旧摘要残留"))
    assert "此前对话摘要" not in captured["prompt"]
    assert "陈旧摘要残留" not in captured["prompt"]


# ── ⑥c 多子目标路径：每节 deep_think prompt 注入同一摘要（review 修复）──


@pytest.mark.asyncio
async def test_synth_answer_multi_goal_injects_summary() -> None:
    captured: dict = {}
    mock_llm = _capturing_synth_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        # 多子目标 + 超窗消息 → _synth_section 的 prompt 捕获摘要
        await synth_answer_node(_state(7, goals=[_subgoal()]))
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


@pytest.mark.asyncio
async def test_synth_answer_multi_goal_short_session_no_summary() -> None:
    captured: dict = {}
    mock_llm = _capturing_synth_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        await synth_answer_node(_state(5, goals=[_subgoal()]))  # 11 条
    assert "此前对话摘要" not in captured["prompt"]
