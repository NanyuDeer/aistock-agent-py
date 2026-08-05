"""synth_answer cards 汇总逻辑单测（P11 线 3，spec §3.2/§3.4）。"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.synth_answer import (
    SynthOutput,
    _build_cards,
    _build_deep_card,
    synth_answer_node,
)
from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.state.chat_schema import DeepReportRef, QuestionState


def _evidence(skill: str, *, raw: dict | None = None,
              symbols: list[str] | None = None) -> Evidence:
    return Evidence(
        facts=["f"],
        sources=[],
        as_of=datetime.now(UTC),
        symbols=symbols or [],
        skill_name=skill,
        raw=raw or {},
    )


def _state(evidences: list[Evidence] | None = None) -> QuestionState:
    return {
        "messages": [HumanMessage(content="q")],
        "goal": InsightGoal(question="q", intent="stock_snapshot"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": evidences or [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }


def _mock_synth_llm(insight_dict: dict) -> MagicMock:
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(
            ainvoke=AsyncMock(
                return_value=SynthOutput.model_validate({"insight": insight_dict})
            )
        )
    )
    return mock_llm


def test_build_cards_multi_skill():
    """多 skill 证据 → 多卡片（非卡片化 skill 跳过）。"""
    evs = [
        _evidence("stock_snapshot",
                  raw={"symbol": "600519", "quote": {"name": "贵州茅台", "code": "600519",
                                                      "price": 1500.0, "change_pct": 1.2}},
                  symbols=["600519"]),
        _evidence("capital_flow",
                  raw={"symbol": "600519", "flow": {"main_in": 1.0, "main_out": 0.5,
                                                    "net_amount": 0.5, "flow_5d": []}},
                  symbols=["600519"]),
        _evidence("market_snapshot",
                  raw={"scope": "both",
                       "a_share_card": {"indices": [{"index_name": "上证指数", "code": "000001.SH",
                                                     "value": 3200.0, "change_pct": 0.5}],
                                        "up_count": 2500, "flat_count": 500,
                                        "down_count": 2000, "trade_date": "20260805"}}),
        _evidence("sector_snapshot", raw={}),  # 非卡片化 → 跳过
    ]
    cards = _build_cards(evs)
    assert cards is not None
    assert [c.card_type for c in cards] == ["stock_snapshot", "capital_flow", "market_snapshot"]
    assert cards[0].data["name"] == "贵州茅台"
    assert cards[1].data["flow_5d"] == []
    assert cards[2].data["up_count"] == 2500


def test_build_cards_single_skill():
    """单 skill 证据 → 单卡片。"""
    cards = _build_cards([
        _evidence("stock_snapshot",
                  raw={"symbol": "600519", "quote": {"name": "贵州茅台", "price": 1500.0}},
                  symbols=["600519"]),
    ])
    assert cards is not None
    assert len(cards) == 1
    assert cards[0].card_type == "stock_snapshot"


def test_build_cards_all_failed_returns_none():
    """全部卡片化证据缺 raw 结构化字段 → None（不破坏对话）。"""
    evs = [
        _evidence("stock_snapshot", raw={"symbol": "600519"}),
        _evidence("capital_flow", raw={"symbol": "600519"}),
        _evidence("market_snapshot", raw={"scope": "global"}),
    ]
    assert _build_cards(evs) is None


def test_build_cards_missing_raw_fields_tolerant():
    """data 缺个别字段 → 卡片仍产出（前端容错渲染）。"""
    cards = _build_cards([
        _evidence("stock_snapshot", raw={"quote": {"price": 1500.0}}, symbols=["600519"]),
    ])
    assert cards is not None
    assert cards[0].data == {"price": 1500.0}


def test_build_cards_market_global_only_no_market_card():
    """market_snapshot 仅 global scope → 无 market 卡片，其余卡片照常。"""
    cards = _build_cards([
        _evidence("market_snapshot", raw={"scope": "global"}),
        _evidence("stock_snapshot", raw={"quote": {"name": "贵州茅台", "price": 1500.0}},
                  symbols=["600519"]),
    ])
    assert cards is not None
    assert [c.card_type for c in cards] == ["stock_snapshot"]


def test_build_cards_comparison_conclusion_from_quotes():
    """comparison 卡片：stocks 透传 parsed，conclusion 从 quotes 里'对比结论'facts 拼接。"""
    cards = _build_cards([
        _evidence("compare_stocks",
                  raw={"quotes": ["贵州茅台(600519): ...", "对比结论：贵州茅台 涨幅最高（+1.20%），"
                                  "五粮液 涨幅最低（-0.80%）"],
                       "compared": ["600519", "000858"], "failed": [],
                       "parsed": [{"name": "贵州茅台", "code": "600519", "price": 1500.0,
                                   "change_pct": 1.2, "available": True}]}),
    ])
    assert cards is not None
    assert cards[0].card_type == "comparison"
    assert cards[0].data["stocks"][0]["available"] is True
    assert cards[0].data["conclusion"].startswith("对比结论")


def test_build_deep_card():
    """DeepReportRef → deep 卡片；None → None。"""
    ref: DeepReportRef = {
        "worker": "stock", "report_id": "1", "question": "q",
        "summary": "s", "symbols": ["600519"], "tag_codes": [], "created_at": "t",
    }
    card = _build_deep_card(ref)
    assert card is not None
    assert card.card_type == "deep"
    assert card.data["worker"] == "stock"
    assert _build_deep_card(None) is None


@pytest.mark.asyncio
async def test_synth_answer_success_branch_cards():
    """LLM 成功路径 → result['cards'] 非 None（由 evidences 汇总）。"""
    ev = _evidence("stock_snapshot",
                   raw={"symbol": "600519", "quote": {"name": "贵州茅台", "code": "600519",
                                                      "price": 1500.0, "change_pct": 1.2}},
                   symbols=["600519"])
    mock_llm = _mock_synth_llm({
        "conclusion": "结论",
        "basis_indices": [1],
        "confidence": "medium",
        "uncertainty": [],
        "answer_mode": "validate",
    })
    with (
        patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm),
        patch("aistock_agent.graph.nodes.synth_answer.trading_session_status",
              return_value=("trading", "")),
    ):
        result = await synth_answer_node(_state([ev]))

    assert result["cards"] is not None
    assert result["cards"][0].card_type == "stock_snapshot"


@pytest.mark.asyncio
async def test_synth_answer_deep_branch_cards():
    """deep 分支 → result['cards'] 为 deep 卡片（复用 last_deep_report）。"""
    state: QuestionState = {
        "messages": [HumanMessage(content="深度分析 600519")],
        "goal": InsightGoal(question="深度分析 600519", intent="stock_snapshot",
                            symbols=["600519"]),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "深度分析报告全文……",
        "trace": None,
        "deep_source": "stock",
    }
    result = await synth_answer_node(state)

    assert result["cards"] is not None
    assert result["cards"][0].card_type == "deep"
    assert result["cards"][0].data["worker"] == "stock"
    assert result["cards"][0].data["question"] == "深度分析 600519"


@pytest.mark.asyncio
async def test_synth_answer_clarification_cards_none():
    """澄清短路 → cards=None。"""
    state: QuestionState = {
        "messages": [HumanMessage(content="q")],
        "goal": InsightGoal(question="q", intent="stock_snapshot"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "clarification": "请提供 6 位股票代码后重试。",
    }
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("deep LLM should not be called"),
    ):
        result = await synth_answer_node(state)
    assert result["cards"] is None


@pytest.mark.asyncio
async def test_synth_answer_exception_branch_cards_none():
    """LLM 异常降级 → cards=None（降级路径不引入新行为）。"""
    ev = _evidence("stock_snapshot", raw={"quote": {"price": 1500.0}}, symbols=["600519"])
    with (
        patch("aistock_agent.graph.nodes.synth_answer.get_deep_think",
              side_effect=RuntimeError("boom")),
        patch("aistock_agent.graph.nodes.synth_answer.trading_session_status",
              return_value=("trading", "")),
    ):
        result = await synth_answer_node(_state([ev]))

    assert result["insight"].answer_mode == "validate"
    assert result["cards"] is None
