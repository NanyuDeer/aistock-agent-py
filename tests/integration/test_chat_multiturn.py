# tests/integration/test_chat_multiturn.py
"""CHAT QA 链路多轮对话集成测试。

验证：
1. checkpointer 状态恢复：同一 thread_id 多次调用，messages 历史累积
2. 上下文延续：qa_router 第二轮能读到第一轮的 HumanMessage + AIMessage
3. synth_answer 追加 AIMessage 到 messages，形成完整对话历史
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.graph.nodes.qa_router import QARouterOutput
from aistock_agent.graph.nodes.synth_answer import SynthOutput
from aistock_agent.schemas.chat_contract import (
    Evidence,
    Insight,
    InsightGoal,
    SkillCall,
)
from aistock_agent.state.chat_schema import QuestionState


def _evidence(skill: str, facts: list[str]) -> Evidence:
    return Evidence(
        facts=facts,
        sources=[],
        as_of=datetime.now(timezone.utc),
        skill_name=skill,
    )


def _qa_output(intent: str, skill: str, args: dict | None = None, **goal_kwargs) -> QARouterOutput:
    return QARouterOutput(
        goal=InsightGoal(question="test", intent=intent, **goal_kwargs),
        plan="direct",
        skill_calls=[SkillCall(skill_name=skill, args=args or {})],
    )


def _synth_output(conclusion: str, mode: str = "validate") -> SynthOutput:
    return SynthOutput(
        insight=Insight(
            conclusion=conclusion,
            basis=[_evidence("test", ["fact"])],
            confidence="medium",
            uncertainty=[],
            answer_mode=mode,
        )
    )


@pytest.mark.asyncio
async def test_multiturn_messages_accumulate():
    """同一 thread_id 两轮对话，messages 累积为 [H1, A1, H2, A2]。"""
    # 第 1 轮：问茅台行情 → stock_snapshot
    # 第 2 轮：问它最近新闻 → stock_news（"它"指代茅台，需历史解析）
    qa_out_1 = _qa_output(
        "stock_snapshot", "stock_snapshot", {"symbol": "600519"},
        symbols=["600519"], time_range="realtime",
    )
    synth_out_1 = _synth_output("茅台当前 1800 元", "trace")

    qa_out_2 = _qa_output(
        "stock_news", "stock_news", {"symbol": "600519"},
        symbols=["600519"],
    )
    synth_out_2 = _synth_output("茅台近期发布半年报", "trace")

    # 按调用顺序：轮1 qa → 轮1 synth → 轮2 qa → 轮2 synth
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_out_1)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_out_1)),
            MagicMock(ainvoke=AsyncMock(return_value=qa_out_2)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_out_2)),
        ]
    )

    # 捕获第 2 轮 qa_router 收到的 messages
    captured_round2_messages: list = []

    async def capture_round2_ainvoke(messages):
        captured_round2_messages.extend(messages)
        return qa_out_2

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(return_value="600519 当前价 1800"),
    ), patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=AsyncMock(return_value="茅台发布半年报"),
    ):
        graph = compile_chat_graph()
        config = {"configurable": {"thread_id": "multiturn-test-1"}}

        # 第 1 轮
        state1: QuestionState = {
            "messages": [HumanMessage(content="茅台现在多少钱")],
            "goal": None,
            "plan": "direct",
            "skill_calls": [],
            "evidences": [],
            "insight": None,
            "final_response": "",
            "trace": None,
        }
        result1 = await graph.ainvoke(state1, config=config)

        # 第 2 轮（同一 thread_id）
        state2: QuestionState = {
            "messages": [HumanMessage(content="它最近有什么新闻")],
            "goal": None,
            "plan": "direct",
            "skill_calls": [],
            "evidences": [],
            "insight": None,
            "final_response": "",
            "trace": None,
        }
        result2 = await graph.ainvoke(state2, config=config)

    # 第 1 轮返回包含 AIMessage
    assert result1["insight"] is not None
    assert result1["final_response"] == "茅台当前 1800 元"

    # 第 2 轮返回包含 AIMessage
    assert result2["insight"] is not None
    assert result2["final_response"] == "茅台近期发布半年报"

    # 验证 checkpointer 累积：最终 state.messages 应为 [H1, A1, H2, A2]
    final_messages = result2.get("messages", [])
    # add_messages reducer 累积后应 4 条
    assert len(final_messages) == 4
    assert isinstance(final_messages[0], HumanMessage)
    assert isinstance(final_messages[1], AIMessage)
    assert isinstance(final_messages[2], HumanMessage)
    assert isinstance(final_messages[3], AIMessage)
    # 内容校验
    assert "茅台现在多少钱" in final_messages[0].content
    assert "1800" in final_messages[1].content
    assert "它最近有什么新闻" in final_messages[2].content
    assert "半年报" in final_messages[3].content


@pytest.mark.asyncio
async def test_multiturn_different_threads_isolated():
    """不同 thread_id 的对话历史相互隔离。"""
    qa_out = _qa_output("report_lookup", "report_lookup", {"report_type": "review", "date": "2026-07-28"})
    synth_out = _synth_output("今日晨报内容")

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=qa_out))
    )

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.report_lookup.get_cached_review",
        new=AsyncMock(return_value={"markdown": "晨报", "trace_summary": "震荡"}),
    ):
        graph = compile_chat_graph()

        # thread A 第一轮
        state_a: QuestionState = {
            "messages": [HumanMessage(content="thread A 问题")],
            "goal": None, "plan": "direct", "skill_calls": [], "evidences": [],
            "insight": None, "final_response": "", "trace": None,
        }
        await graph.ainvoke(state_a, config={"configurable": {"thread_id": "thread-A"}})

        # thread B 第一轮（不同 thread_id）
        state_b: QuestionState = {
            "messages": [HumanMessage(content="thread B 问题")],
            "goal": None, "plan": "direct", "skill_calls": [], "evidences": [],
            "insight": None, "final_response": "", "trace": None,
        }
        result_b = await graph.ainvoke(state_b, config={"configurable": {"thread_id": "thread-B"}})

    # thread B 的 messages 不应包含 thread A 的内容
    final_messages = result_b.get("messages", [])
    # 仅 [H_B, A_B]，不应有 thread A 的消息
    assert len(final_messages) == 2
    assert "thread B" in final_messages[0].content
    assert "thread A" not in final_messages[1].content


@pytest.mark.asyncio
async def test_multiturn_qa_router_receives_history():
    """第 2 轮 qa_router 收到的 messages 包含第 1 轮的完整历史。"""
    qa_out_1 = _qa_output("stock_snapshot", "stock_snapshot", {"symbol": "600519"}, symbols=["600519"])
    synth_out_1 = _synth_output("茅台 1800 元")

    # 第 2 轮 qa_router 用捕获
    captured: list = []

    async def capture_round2(messages):
        captured.extend(messages)
        return _qa_output("stock_news", "stock_news", {"symbol": "600519"}, symbols=["600519"])

    qa_out_2_mock = MagicMock()

    synth_out_2 = _synth_output("茅台半年报")

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_out_1)),       # 轮1 qa
            MagicMock(ainvoke=AsyncMock(return_value=synth_out_1)),    # 轮1 synth
            MagicMock(ainvoke=capture_round2),                          # 轮2 qa（捕获）
            MagicMock(ainvoke=AsyncMock(return_value=synth_out_2)),    # 轮2 synth
        ]
    )

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(return_value="600519 当前价 1800"),
    ), patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=AsyncMock(return_value="茅台半年报"),
    ):
        graph = compile_chat_graph()
        config = {"configurable": {"thread_id": "multiturn-test-3"}}

        await graph.ainvoke(
            {"messages": [HumanMessage(content="茅台现在多少钱")], "goal": None, "plan": "direct",
             "skill_calls": [], "evidences": [], "insight": None, "final_response": "", "trace": None},
            config=config,
        )
        await graph.ainvoke(
            {"messages": [HumanMessage(content="它最近有什么新闻")], "goal": None, "plan": "direct",
             "skill_calls": [], "evidences": [], "insight": None, "final_response": "", "trace": None},
            config=config,
        )

    # 第 2 轮 qa_router 收到的 messages 应包含历史
    # messages = [SystemPrompt, H1, A1, H2]（SystemPrompt 是 qa_router 内部加的）
    assert len(captured) >= 3  # SystemPrompt + H1 + A1 + H2 至少
    # 应包含第 1 轮的 HumanMessage 和 AIMessage
    contents = [m.content for m in captured if hasattr(m, "content")]
    assert any("茅台现在多少钱" in c for c in contents)
    assert any("1800" in c for c in contents)
    assert any("它最近有什么新闻" in c for c in contents)
