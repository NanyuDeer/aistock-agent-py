"""QA Router 节点单元测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.qa_router import (
    KEYWORD_FALLBACK,
    QARouterOutput,
    qa_router_node,
    route_by_keyword_fallback,
)
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState


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
    }


def test_route_by_keyword_fallback_report():
    call = route_by_keyword_fallback("今天晨报说了什么")
    assert call.skill_name == "report_lookup"


def test_route_by_keyword_fallback_stock():
    call = route_by_keyword_fallback("茅台现在多少钱")
    assert call.skill_name == "stock_snapshot"


def test_route_by_keyword_fallback_news():
    call = route_by_keyword_fallback("茅台最近新闻")
    assert call.skill_name == "stock_news"


def test_route_by_keyword_fallback_trace():
    call = route_by_keyword_fallback("今天为什么涨")
    assert call.skill_name == "trace_lookup"


def test_route_by_keyword_fallback_industry():
    call = route_by_keyword_fallback("白酒板块上下游")
    assert call.skill_name == "industry_relation"


def test_route_by_keyword_fallback_default_report():
    """无匹配关键词 → 默认 report_lookup。"""
    call = route_by_keyword_fallback("随机问题xyz")
    assert call.skill_name == "report_lookup"


@pytest.mark.asyncio
async def test_qa_router_llm_success_direct():
    """LLM 成功返回 direct 计划。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="茅台现在多少钱", intent="stock_snapshot", symbols=["600519"]),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output)))
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("茅台现在多少钱"))
    assert result["plan"] == "direct"
    assert len(result["skill_calls"]) == 1
    assert result["skill_calls"][0].skill_name == "stock_snapshot"
    assert result["goal"].symbols == ["600519"]


@pytest.mark.asyncio
async def test_qa_router_llm_failure_fallback():
    """LLM 异常 → 关键词兜底 + degraded 标记。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down"))))
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("今天晨报说了什么"))
    assert result["plan"] == "direct"
    assert result["skill_calls"][0].skill_name == "report_lookup"
    # 兜底标记：goal.constraints 含 router_fallback
    assert result["goal"].constraints.get("router_fallback") == "true"
