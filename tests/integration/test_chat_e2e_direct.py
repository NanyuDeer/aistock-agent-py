# tests/integration/test_chat_e2e_direct.py
"""CHAT QA 链路端到端测试 — direct 路径，5 种 intent 各 1 个用例。"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.chat_builder import compile_chat_graph
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


def _mock_llm_output(
    intent: str,
    skill: str,
    conclusion: str,
    mode: str = "validate",
    *,
    goal_kwargs: dict | None = None,
    skill_args: dict | None = None,
):
    """构造 mock 的 LLM 输出。"""
    from aistock_agent.graph.nodes.qa_router import QARouterOutput
    from aistock_agent.graph.nodes.synth_answer import SynthOutput

    goal = InsightGoal(question="test", intent=intent, **(goal_kwargs or {}))
    qa_output = QARouterOutput(
        goal=goal,
        plan="direct",
        skill_calls=[SkillCall(skill_name=skill, args=skill_args or {})],
    )
    synth_output = SynthOutput(
        insight=Insight(
            conclusion=conclusion,
            basis=[_evidence(skill, ["fact1"])],
            confidence="medium",
            uncertainty=[],
            answer_mode=mode,
        )
    )
    return qa_output, synth_output


@pytest.mark.asyncio
async def test_e2e_report_lookup():
    qa_out, synth_out = _mock_llm_output(
        "report_lookup", "report_lookup", "今日晨报显示市场震荡", "validate"
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_out)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_out)),
        ]
    )
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.report_lookup.get_cached_review",
        new=AsyncMock(return_value={"markdown": "晨报内容", "trace_summary": "震荡"}),
    ):
        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="今天晨报说了什么")],
            "goal": None,
            "plan": "direct",
            "skill_calls": [],
            "evidences": [],
            "insight": None,
            "final_response": "",
            "trace": None,
        }
        result = await graph.ainvoke(state)

    assert result["insight"] is not None
    assert result["insight"].answer_mode == "validate"
    assert len(result["evidences"]) == 1
    assert result["evidences"][0].skill_name == "report_lookup"
    assert result["trace"] is not None
    assert result["trace"].actual_mode == "validate"


@pytest.mark.asyncio
async def test_e2e_stock_snapshot():
    qa_out, synth_out = _mock_llm_output(
        "stock_snapshot", "stock_snapshot", "茅台当前 1800 元", "validate"
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_out)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_out)),
        ]
    )
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(return_value="600519 当前价 1800"),
    ):
        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="茅台现在多少钱")],
            "goal": None,
            "plan": "direct",
            "skill_calls": [],
            "evidences": [],
            "insight": None,
            "final_response": "",
            "trace": None,
        }
        result = await graph.ainvoke(state)

    assert result["insight"] is not None
    assert len(result["evidences"]) == 1
    assert result["evidences"][0].skill_name == "stock_snapshot"


@pytest.mark.asyncio
async def test_e2e_stock_news():
    qa_out, synth_out = _mock_llm_output(
        "stock_news", "stock_news", "茅台近期发布半年报", "trace",
        skill_args={"symbol": "600519"},
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_out)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_out)),
        ]
    )
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=AsyncMock(return_value="茅台发布半年报"),
    ):
        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="茅台最近新闻")],
            "goal": None,
            "plan": "direct",
            "skill_calls": [],
            "evidences": [],
            "insight": None,
            "final_response": "",
            "trace": None,
        }
        result = await graph.ainvoke(state)

    assert result["insight"] is not None
    assert result["insight"].answer_mode == "trace"


@pytest.mark.asyncio
async def test_e2e_trace_lookup():
    from unittest.mock import MagicMock as _MagicMock

    fake_snapshot = _MagicMock()
    fake_trace = _MagicMock()
    fake_trace.attribution_status = "confirmed"
    fake_trace.confidence = "medium"
    fake_trace.unresolved_questions = []
    fake_trace.candidates = []
    fake_trace.primary_chain_id = "chain_1"

    qa_out, synth_out = _mock_llm_output(
        "trace_lookup", "trace_lookup", "今日上涨由白酒板块带动", "trace"
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_out)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_out)),
        ]
    )
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.trace_lookup.load_validated_trace",
        new=AsyncMock(return_value=(fake_snapshot, fake_trace)),
    ):
        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="今天为什么涨")],
            "goal": None,
            "plan": "direct",
            "skill_calls": [],
            "evidences": [],
            "insight": None,
            "final_response": "",
            "trace": None,
        }
        result = await graph.ainvoke(state)

    assert result["insight"] is not None
    assert result["insight"].answer_mode == "trace"


@pytest.mark.asyncio
async def test_e2e_industry_relation():
    qa_out, synth_out = _mock_llm_output(
        "industry_relation", "industry_relation", "白酒上下游为食品饮料", "trace",
        skill_args={"keywords": ["白酒"]},
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_out)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_out)),
        ]
    )
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.industry_relation.match_industry_by_keywords",
        new=AsyncMock(return_value="白酒 → 食品饮料"),
    ):
        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="白酒板块上下游")],
            "goal": None,
            "plan": "direct",
            "skill_calls": [],
            "evidences": [],
            "insight": None,
            "final_response": "",
            "trace": None,
        }
        result = await graph.ainvoke(state)

    assert result["insight"] is not None
    assert result["insight"].answer_mode == "trace"
