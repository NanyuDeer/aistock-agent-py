"""QA Router 2026-08-05 验收补丁测试（问题 8/11）。

- 问题 8：对比问句（"茅台和五粮液哪个更好"）应短路 compare_stocks，不再落单名澄清
- 问题 11：名称候选不被意图词/连接词污染（"宁德时代最近有什么新闻" → "宁德时代"）
"""
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.graph.nodes.qa_router import (
    _extract_multi_name_candidates,
    _extract_stock_name_candidate,
    qa_router_node,
)
from aistock_agent.state.chat_schema import QuestionState


@pytest.fixture(autouse=True)
def _no_real_node_resolve(monkeypatch):
    """默认 mock Node 名称解析，由用例显式 side_effect。"""
    monkeypatch.setattr(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value=None),
    )


def _state(message: str) -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "complexity": None,
        "force_deep": None,
    }


# ── 问题 11：停用词表补全后名称候选提取 ──


def test_name_candidate_clean_news_sentence():
    """「宁德时代最近有什么新闻」→ 候选名应为「宁德时代」（新闻/有/什么等已去除）。"""
    assert _extract_stock_name_candidate("宁德时代最近有什么新闻") == "宁德时代"


def test_name_candidate_clean_shuo_shi_sentence():
    """「我说的是宁德时代」→ 候选名应为「宁德时代」（是/说/我/的 已去除）。"""
    assert _extract_stock_name_candidate("我说的是宁德时代") == "宁德时代"


def test_name_candidate_clean_compare_sentence():
    """「茅台和五粮液哪个更好」→ 候选名不应再是整句（"哪个/更好"已去除）。"""
    candidate = _extract_stock_name_candidate("茅台和五粮液哪个更好")
    # "和" 保留给切分路径，单候选路径至少不应包含"哪个更好"
    assert "哪个" not in candidate and "更好" not in candidate


# ── 问题 8：多标的候选提取（分隔符切分）──


def test_extract_multi_name_candidates_and_separator():
    """「茅台和五粮液哪个更好」→ 切分出 茅台 / 五粮液。"""
    names = _extract_multi_name_candidates("茅台和五粮液哪个更好")
    assert "茅台" in names
    assert "五粮液" in names


def test_extract_multi_name_candidates_vs_separator():
    """「宁德时代 vs 比亚迪」→ 切分出 宁德时代 / 比亚迪。"""
    names = _extract_multi_name_candidates("宁德时代 vs 比亚迪")
    assert "宁德时代" in names
    assert "比亚迪" in names


# ── 问题 8：对比闸门短路 compare_stocks ──


@pytest.mark.asyncio
async def test_compare_gate_short_circuit_multi_names():
    """「茅台和五粮液哪个更好」→ compare_stocks(symbols=[600519, 000858])，不走澄清。"""
    resolve_side_effect = {
        "茅台": "600519",
        "五粮液": "000858",
    }
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(side_effect=lambda name: resolve_side_effect.get(name)),
    ):
        result = await qa_router_node(_state("茅台和五粮液哪个更好"))
    assert result["plan"] == "direct"
    assert len(result["skill_calls"]) == 1
    call = result["skill_calls"][0]
    assert call.skill_name == "compare_stocks"
    assert set(call.args["symbols"]) == {"600519", "000858"}
    assert "clarification" not in result


@pytest.mark.asyncio
async def test_compare_gate_short_circuit_code_plus_name():
    """「600519 和五粮液哪个更好」→ compare_stocks(symbols=[600519, 000858])。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(side_effect=lambda name: {"五粮液": "000858"}.get(name)),
    ):
        result = await qa_router_node(_state("600519 和五粮液哪个更好"))
    assert result["plan"] == "direct"
    call = result["skill_calls"][0]
    assert call.skill_name == "compare_stocks"
    assert set(call.args["symbols"]) == {"600519", "000858"}


@pytest.mark.asyncio
async def test_compare_gate_falls_through_when_not_enough():
    """对比词命中但 <2 个标的（resolve 失败）→ 不短路 compare_stocks，走后续逻辑。"""
    result = await qa_router_node(_state("茅台和五粮液哪个更好"))
    # resolve 全部失败 → 不应有 compare_stocks call
    assert all(c.skill_name != "compare_stocks" for c in result.get("skill_calls", []))


# ── 问题 11：新闻问句走名称解析 → stock_news ──


@pytest.mark.asyncio
async def test_stock_news_resolves_clean_name():
    """「宁德时代最近有什么新闻」→ resolve("宁德时代") → stock_news(limit=10)。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value="300750"),
    ):
        result = await qa_router_node(_state("宁德时代最近有什么新闻"))
    assert "clarification" not in result
    call = result["skill_calls"][0]
    assert call.skill_name == "stock_news"
    assert call.args == {"symbol": "300750", "limit": 10}


# ── 问题 14（前端复测）：多轮指代兜底——LLM 失败时复用上一轮标的 ──


def _multiturn_state() -> QuestionState:
    return {
        "messages": [
            HumanMessage(content="贵州茅台今天怎么样"),
            AIMessage(content="贵州茅台报1328元"),
            HumanMessage(content="它今天的成交量呢"),
        ],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "complexity": None,
        "force_deep": None,
    }


@pytest.mark.asyncio
async def test_multiturn_fallback_reuses_prev_symbol_when_llm_fails():
    """LLM 失败 + 当前消息指代 + 上一轮有标的 → 复用上一轮 symbol（600519），不澄清。"""
    def fake_resolve(name: str) -> str | None:
        return "600519" if "茅台" in name else None

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=RuntimeError("llm down"),
    ), patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(side_effect=fake_resolve),
    ):
        result = await qa_router_node(_multiturn_state())
    assert "clarification" not in result
    call = result["skill_calls"][0]
    assert call.skill_name == "stock_snapshot"
    assert call.args == {"symbol": "600519"}
    assert result["goal"].constraints.get("multiturn_ref") == "true"


@pytest.mark.asyncio
async def test_multiturn_fallback_not_triggered_without_prev_symbol():
    """LLM 失败但上一轮无标的（如"今天大盘怎么样"）→ 不触发多轮指代复用，不误伤。"""
    def fake_resolve(name: str) -> str | None:
        return None  # 上一轮"大盘"也解析不到 symbol

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=RuntimeError("llm down"),
    ), patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(side_effect=fake_resolve),
    ):
        state = _multiturn_state()
        state["messages"] = [
            HumanMessage(content="今天大盘怎么样"),
            AIMessage(content="上证3832点"),
            HumanMessage(content="它今天的成交量呢"),
        ]
        result = await qa_router_node(state)
    # 不应出现 multiturn_ref 复用（上一轮无个股标的）
    assert result["goal"].constraints.get("multiturn_ref") is None


@pytest.mark.asyncio
async def test_multiturn_fallback_not_triggered_on_first_turn():
    """首轮（无历史）LLM 失败 → 不触发多轮指代复用（保持既有澄清/兜底）。"""
    def fake_resolve(name: str) -> str | None:
        return "600519" if "茅台" in name else None

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=RuntimeError("llm down"),
    ), patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(side_effect=fake_resolve),
    ):
        result = await qa_router_node(_state("它今天的成交量呢"))
    # 首轮无历史 → 复用不生效
    assert result["goal"].constraints.get("multiturn_ref") is None
