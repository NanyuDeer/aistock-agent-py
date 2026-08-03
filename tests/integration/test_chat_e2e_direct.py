# tests/integration/test_chat_e2e_direct.py
"""CHAT QA 链路端到端测试 — direct 路径，5 种 intent 各 1 个用例。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.schemas.chat_contract import (
    InsightGoal,
    SkillCall,
)
from aistock_agent.state.chat_schema import QuestionState


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
    from aistock_agent.graph.nodes.synth_answer import SynthInsightOutput, SynthOutput

    goal = InsightGoal(question="test", intent=intent, **(goal_kwargs or {}))
    qa_output = QARouterOutput(
        goal=goal,
        plan="direct",
        skill_calls=[SkillCall(skill_name=skill, args=skill_args or {})],
        complexity="light",
    )
    synth_output = SynthOutput(
        insight=SynthInsightOutput(
            conclusion=conclusion,
            basis_indices=[1],
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
        "stock_snapshot", "stock_snapshot", "茅台当前 1800 元", "validate",
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
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        new=AsyncMock(return_value="600519"),
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
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        new=AsyncMock(return_value="600519"),
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
async def test_e2e_evidence_resolver():
    """evidence_resolver direct E2E 路径。"""
    from unittest.mock import MagicMock as _MagicMock

    fake_snapshot = _MagicMock()
    fake_trace = _MagicMock()
    fake_trace.attribution_status = "confirmed"
    fake_trace.confidence = "medium"
    fake_trace.unresolved_questions = []
    fake_trace.candidates = []
    fake_trace.primary_chain_id = "chain_1"

    qa_out, synth_out = _mock_llm_output(
        "evidence_resolver", "evidence_resolver", "市场证据显示今日上涨有明确归因", "trace"
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
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
        new=AsyncMock(return_value=(fake_snapshot, fake_trace)),
    ):
        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="有什么证据说明今天市场走势")],
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
    assert result["evidences"][0].skill_name == "evidence_resolver"
    assert result["evidences"][0].degraded is False


@pytest.mark.asyncio
async def test_e2e_sector_snapshot():
    """sector_snapshot direct E2E 路径。"""
    qa_out, synth_out = _mock_llm_output(
        "sector_snapshot", "sector_snapshot", "今日半导体板块领涨", "trace",
        skill_args={},
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
        "aistock_agent.skills.sector_snapshot.node_api",
    ) as mock_api:
        mock_api.get = AsyncMock(return_value={
            "update_time": "2026-07-30 10:30",
            "hot_sectors": [
                {"name": "半导体", "today_change": 3.2, "leading_stock": "中芯国际",
                 "main_stocks": [{"code": "688981", "name": "中芯国际", "change_pct": 8.5}]},
            ],
        })
        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="板块强弱分析")],
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
    ev = result["evidences"][0]
    assert ev.skill_name == "sector_snapshot"
    assert ev.degraded is False
    assert any("realtime_quote" == s.kind for s in ev.sources)
    assert len(ev.facts) > 0
    assert ev.raw != {}


@pytest.mark.asyncio
async def test_e2e_market_snapshot():
    """market_snapshot direct E2E 路径。"""
    qa_out, synth_out = _mock_llm_output(
        "market_snapshot", "market_snapshot", "今日大盘震荡上行", "validate",
        skill_args={"scope": "both", "snapshot_kind": "quick"},
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
        "aistock_agent.skills.market_snapshot.node_api",
    ) as mock_api, patch(
        "aistock_agent.skills.market_snapshot.asyncio.to_thread",
    ) as mock_to_thread:
        mock_api.get_quick_snapshot = AsyncMock(return_value={
            "schema_version": "1.0", "status": "complete", "snapshot_kind": "quick",
            "trade_date": "20260730", "captured_at": "2026-07-30T07:30:00.000Z",
            "indexes": [{"ts_code": "000001.SH", "name": "上证指数", "close": 3200.0,
                         "pct_chg": 0.5, "amount": 100000.0}],
            "breadth": {"total_count": 5000, "advance_count": 2500, "decline_count": 2000,
                        "flat_count": 500, "advance_ratio": 0.5},
            "turnover": {"amount_yuan": 95_000_000_000, "change_pct": 5.0},
            "limits": {"up_count": 20, "down_count": 15, "broken_count": 5, "highest_board": 3},
            "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
            "sectors": {"top_gainers": [], "top_losers": [], "top_inflows": [], "top_outflows": []},
        })
        mock_to_thread.side_effect = lambda fn, arg: [
            {"ticker": "^GSPC", "name": "标普500", "price": 5500.0, "change_pct": 0.36},
        ]

        graph = compile_chat_graph(checkpointer=None)
        state: QuestionState = {
            "messages": [HumanMessage(content="大盘今天走势如何")],
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
    ev = result["evidences"][0]
    assert ev.skill_name == "market_snapshot"
    assert ev.degraded is False
    assert any("realtime_quote" == s.kind for s in ev.sources)
    assert len(ev.facts) > 0
    assert ev.raw != {}


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
        "aistock_agent.skills.evidence_resolver.load_validated_trace",
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
    # basis 由服务端从 state.evidences 映射，与 skill_executor 产出完全一致
    assert result["insight"].basis == result["evidences"]


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
