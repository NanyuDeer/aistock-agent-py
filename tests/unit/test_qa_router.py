"""QA Router 节点单元测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from aistock_agent.graph.nodes.qa_router import (
    SYSTEM_PROMPT,
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
    call = route_by_keyword_fallback("600519 现在多少钱")
    assert call.skill_name == "stock_snapshot"
    assert call.args == {"symbol": "600519"}


def test_route_by_keyword_fallback_news():
    call = route_by_keyword_fallback("sh600519 最近新闻")
    assert call.skill_name == "stock_news"
    assert call.args == {"symbol": "600519", "limit": 10}


def test_route_by_keyword_fallback_trace():
    call = route_by_keyword_fallback("今天为什么涨")
    assert call.skill_name == "trace_lookup"


def test_route_by_keyword_fallback_industry():
    call = route_by_keyword_fallback("白酒板块上下游")
    assert call.skill_name == "industry_relation"


def test_route_by_keyword_fallback_evidence():
    call = route_by_keyword_fallback("有什么证据")
    assert call.skill_name == "evidence_resolver"


def test_route_by_keyword_fallback_sector():
    call = route_by_keyword_fallback("板块强弱分析")
    assert call.skill_name == "sector_snapshot"


def test_route_by_keyword_fallback_market():
    call = route_by_keyword_fallback("大盘今天怎么样")
    assert call.skill_name == "market_snapshot"


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
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
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
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("今天晨报说了什么"))
    assert result["plan"] == "direct"
    assert result["skill_calls"][0].skill_name == "report_lookup"
    # 兜底标记：goal.constraints 含 router_fallback
    assert result["goal"].constraints.get("router_fallback") == "true"


@pytest.mark.asyncio
async def test_qa_router_llm_failure_evidence_resolver():
    """LLM 异常 → evidence_resolver 关键词兜底。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("有什么证据证明"))
    assert result["skill_calls"][0].skill_name == "evidence_resolver"
    assert result["goal"].intent == "evidence_resolver"
    assert result["goal"].constraints.get("router_fallback") == "true"


@pytest.mark.asyncio
async def test_qa_router_llm_failure_sector_snapshot():
    """LLM 异常 → sector_snapshot 关键词兜底。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("板块强弱分析今天"))
    assert result["skill_calls"][0].skill_name == "sector_snapshot"
    assert result["goal"].intent == "sector_snapshot"
    assert result["goal"].constraints.get("router_fallback") == "true"


@pytest.mark.asyncio
async def test_qa_router_llm_failure_market_snapshot():
    """LLM 异常 → market_snapshot 关键词兜底。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("大盘今天走势如何"))
    assert result["skill_calls"][0].skill_name == "market_snapshot"
    assert result["goal"].intent == "market_snapshot"
    assert result["goal"].constraints.get("router_fallback") == "true"


def test_keyword_fallback_stock_news_extracts_six_digit_symbol() -> None:
    call = route_by_keyword_fallback("600519 最近新闻")
    assert call is not None
    assert call.args == {"symbol": "600519", "limit": 10}


def test_keyword_fallback_stock_news_without_symbol_returns_none() -> None:
    assert route_by_keyword_fallback("茅台最近新闻") is None


def test_keyword_fallback_industry_keeps_keyword_list() -> None:
    call = route_by_keyword_fallback("白酒板块上下游")
    assert call is not None
    assert call.args["keywords"] == ["白酒板块上下游"]


def test_system_prompt_declares_full_json_contract() -> None:
    """Prompt 声明完整 JSON 契约并明确禁止旧字段 skill/params。"""
    assert '"goal"' in SYSTEM_PROMPT
    assert '"plan"' in SYSTEM_PROMPT
    assert '"skill_calls"' in SYSTEM_PROMPT
    assert '"skill_name"' in SYSTEM_PROMPT
    assert '"args"' in SYSTEM_PROMPT
    assert '"depends_on"' in SYSTEM_PROMPT
    assert "禁止使用旧字段 skill、params" in SYSTEM_PROMPT
    assert "不得省略 goal" in SYSTEM_PROMPT


def test_qarouter_output_rejects_legacy_top_level_skill_params() -> None:
    """生产旧形状（顶层 skill/params、缺 goal）被 QARouterOutput 严格拒绝。"""
    legacy_payload = {
        "skill": "stock_snapshot",
        "params": {"symbol": "600519"},
        "plan": "direct",
    }
    with pytest.raises(ValidationError):
        QARouterOutput.model_validate(legacy_payload)


def test_qarouter_output_rejects_legacy_skill_call_fields() -> None:
    """skill_calls 内使用旧字段 skill/params 同样被严格拒绝。"""
    legacy_payload = {
        "goal": InsightGoal(question="茅台现在多少钱", intent="stock_snapshot"),
        "plan": "direct",
        "skill_calls": [{"skill": "stock_snapshot", "params": {"symbol": "600519"}}],
    }
    with pytest.raises(ValidationError):
        QARouterOutput.model_validate(legacy_payload)


@pytest.mark.asyncio
async def test_qa_router_parse_error_still_falls_back_safely() -> None:
    """真实解析异常（Pydantic ValidationError）→ 关键词兜底，不中断图执行。"""

    def _raise_parse_error(*args, **kwargs):
        # 模拟 json_mode 对非契约输出的真实校验失败
        QARouterOutput.model_validate({"plan": "direct"})  # 缺 goal/skill_calls

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_raise_parse_error))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("今天晨报说了什么"))
    assert result["plan"] == "direct"
    assert result["skill_calls"][0].skill_name == "report_lookup"
    assert result["goal"].constraints.get("router_fallback") == "true"


def test_keyword_fallback_index_route_to_market_snapshot() -> None:
    """指数名（创业板指）→ market_snapshot，args 携带 index_name。"""
    call = route_by_keyword_fallback("创业板指今天表现如何")
    assert call is not None
    assert call.skill_name == "market_snapshot"
    assert call.args.get("index_name") == "创业板指"


def test_keyword_fallback_index_name_variants() -> None:
    """指数别名（沪指/深成指/科创50/沪深300/恒生）均可识别。"""
    cases = {
        "沪指今天怎么样": "上证指数",
        "深成指走势如何": "深证成指",
        "科创50表现如何": "科创50",
        "沪深300今天行情": "沪深300",
        "恒生指数今天如何": "恒生指数",
    }
    for msg, expected in cases.items():
        call = route_by_keyword_fallback(msg)
        assert call is not None, msg
        assert call.args.get("index_name") == expected, msg


def test_extract_report_date_explicit() -> None:
    """显式 YYYY-MM-DD / YYYYMMDD 日期提取。"""
    from aistock_agent.graph.nodes.qa_router import extract_report_date

    assert extract_report_date("2026-07-31 大盘为什么涨") == "2026-07-31"
    assert extract_report_date("20260731复盘报告") == "2026-07-31"


def test_extract_report_date_relative_and_fallback() -> None:
    """相对日期与非遗日回退。"""
    from datetime import date

    from aistock_agent.graph.nodes.qa_router import extract_report_date

    # 无显式日期 → 返回今天（或非遗日回退最近交易日）
    result = extract_report_date("复盘报告有哪些未解决问题")
    assert date.fromisoformat(result) is not None  # 合法日期
