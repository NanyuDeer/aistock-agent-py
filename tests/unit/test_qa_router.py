"""QA Router 节点单元测试。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from pydantic import ValidationError

from aistock_agent.graph.nodes.qa_router import (
    SYSTEM_PROMPT,
    QARouterOutput,
    _postprocess_skill_calls,
    qa_router_node,
    route_by_keyword_fallback,
)
from aistock_agent.prompts.general.system import (
    CAPABILITY_REPLY,
    COMPLIANCE_REPLY,
    EDUCATION_REPLY,
)
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall, SubGoal
from aistock_agent.state.chat_schema import QuestionState


@pytest.fixture(autouse=True)
def _no_real_node_resolve(monkeypatch):
    """所有 qa_router 单测默认不调真实 Node 名称解析端点（M4），由用例显式 mock。"""
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
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("600519 现在多少钱"))
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


@pytest.mark.asyncio
async def test_postprocess_normalizes_market_snapshot_args():
    """D27：LLM 输出的非法 scope/snapshot_kind 归一化为默认值，避免 market_snapshot 硬降级。"""
    output = QARouterOutput(
        goal=InsightGoal(intent="market_snapshot", question="大盘怎么样"),
        plan="direct",
        skill_calls=[
            SkillCall(
                skill_name="market_snapshot",
                args={"scope": "all", "snapshot_kind": "quick_full"},
            )
        ],
        complexity="light",
    )
    result = await _postprocess_skill_calls(output, "大盘怎么样", _state("大盘怎么样"))
    call = result.skill_calls[0]
    assert call.args["scope"] == "both"
    assert call.args["snapshot_kind"] == "quick"


@pytest.mark.asyncio
async def test_postprocess_keeps_valid_market_snapshot_args():
    """D27：合法 scope/snapshot_kind 不被改动。"""
    output = QARouterOutput(
        goal=InsightGoal(intent="market_snapshot", question="大盘怎么样"),
        plan="direct",
        skill_calls=[
            SkillCall(
                skill_name="market_snapshot",
                args={"scope": "a_share", "snapshot_kind": "full"},
            )
        ],
        complexity="light",
    )
    result = await _postprocess_skill_calls(output, "大盘怎么样", _state("大盘怎么样"))
    call = result.skill_calls[0]
    assert call.args["scope"] == "a_share"
    assert call.args["snapshot_kind"] == "full"


@pytest.mark.asyncio
async def test_qa_router_index_gate_short_circuits() -> None:
    """指数名 → 闸门 1 短路：不调 LLM、market_snapshot，index_name 透传 constraints。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=AssertionError("LLM should not be called on index gate"),
    ):
        result = await qa_router_node(_state("沪指今天怎么样"))
    assert result["skill_calls"][0].skill_name == "market_snapshot"
    assert result["skill_calls"][0].args.get("index_name") == "上证指数"
    assert result["goal"].constraints.get("index_name") == "上证指数"


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
    """显式 YYYY-MM-DD / YYYYMMDD 日期提取（确定性验证，不受"今天"日期影响）。"""
    from datetime import date

    from aistock_agent.graph.nodes.qa_router import extract_report_date

    assert extract_report_date("2026-07-31 大盘为什么涨") == "2026-07-31"
    # 紧凑格式 YYYYMMDD：patch 日期源为交易日 2026-08-03（周一），
    # 避免回退路径"恰好等于今天"的巧合性让测试假通过
    with patch("aistock_agent.utils.date.shanghai_today", return_value=date(2026, 8, 3)):
        assert extract_report_date("20260731复盘报告") == "2026-07-31"


def test_extract_report_date_relative_and_fallback() -> None:
    """相对日期与非遗日回退。"""
    from datetime import date

    from aistock_agent.graph.nodes.qa_router import extract_report_date

    # 无显式日期且今天是周六（2026-08-01 非遗日）→ 回退最近交易日 2026-07-31
    with patch("aistock_agent.utils.date.shanghai_today", return_value=date(2026, 8, 1)):
        result = extract_report_date("复盘报告有哪些未解决问题")
    assert result == "2026-07-31"


def test_build_compose_plan_market_mainline() -> None:
    """市场主线 → market_snapshot + sector_snapshot compose。"""
    from aistock_agent.graph.nodes.qa_router import build_compose_plan

    plan = build_compose_plan("帮我梳理今天的市场主线")
    assert plan is not None
    assert len(plan) == 2
    assert {c.skill_name for c in plan} == {"market_snapshot", "sector_snapshot"}
    assert plan[0].depends_on == []


def test_build_compose_plan_risk() -> None:
    """风险提示 → market_snapshot + sector_snapshot compose。"""
    from aistock_agent.graph.nodes.qa_router import build_compose_plan

    plan = build_compose_plan("市场有哪些风险提示")
    assert plan is not None
    assert len(plan) == 2
    assert {c.skill_name for c in plan} == {"market_snapshot", "sector_snapshot"}


def test_build_compose_plan_none_for_normal_question() -> None:
    """普通问题不触发 compose。"""
    from aistock_agent.graph.nodes.qa_router import build_compose_plan

    assert build_compose_plan("茅台今天行情怎么样") is None


@pytest.mark.asyncio
async def test_qa_router_compose_gate_short_circuits() -> None:
    """市场主线 → 闸门 3 compose 短路：不调 LLM、直接 market_snapshot + sector_snapshot。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=AssertionError("LLM should not be called on compose gate"),
    ):
        result = await qa_router_node(_state("帮我梳理今天的市场主线"))
    assert result["plan"] == "compose"
    assert len(result["skill_calls"]) == 2
    assert {c.skill_name for c in result["skill_calls"]} == {"market_snapshot", "sector_snapshot"}
    assert result["goal"].constraints.get("gate") == "compose"


# ─── M1 护栏（D29/D32/科普/名称解析/后处理） ───


@pytest.mark.asyncio
async def test_guardrail_compliance_short_circuits() -> None:
    """敏感词命中 → 合规话术，不调 LLM、无 skill_calls。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=AssertionError("LLM should not be called on compliance gate"),
    ):
        result = await qa_router_node(_state("我能买茅台吗"))
    assert result["final_response"] == COMPLIANCE_REPLY
    assert result["skill_calls"] == []
    assert result["goal"].constraints.get("guardrail") == "compliance"


@pytest.mark.asyncio
async def test_guardrail_greeting_short_circuits() -> None:
    """寒暄命中 → 能力介绍话术，零 LLM。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=AssertionError("LLM should not be called on greeting gate"),
    ):
        result = await qa_router_node(_state("你好，在吗"))
    assert result["final_response"] == CAPABILITY_REPLY
    assert result["skill_calls"] == []
    assert result["goal"].constraints.get("guardrail") == "greeting"


@pytest.mark.asyncio
async def test_guardrail_education_short_circuits() -> None:
    """科普问句（"什么是市盈率"）→ 科普引导话术，零 LLM、不兜底 report_lookup。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=AssertionError("LLM should not be called on education gate"),
    ):
        result = await qa_router_node(_state("什么是市盈率"))
    assert result["final_response"] == EDUCATION_REPLY
    assert result["skill_calls"] == []
    assert result["goal"].constraints.get("guardrail") == "education"


@pytest.mark.asyncio
async def test_guardrail_priority_compliance_over_greeting() -> None:
    """优先级链：敏感闸门 > 寒暄闸门（"你好能买吗" → 合规话术）。"""
    result = await qa_router_node(_state("你好，我能买茅台吗"))
    assert result["final_response"] == COMPLIANCE_REPLY
    assert result["goal"].constraints.get("guardrail") == "compliance"


@pytest.mark.asyncio
async def test_resolve_symbol_success_routes_to_stock_snapshot() -> None:
    """'茅台今天怎么样' → resolve 600519 → stock_snapshot(symbol=600519)，不调 LLM。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=AssertionError("LLM should not be called on stock resolve gate"),
    ), patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value="600519"),
    ):
        result = await qa_router_node(_state("茅台今天怎么样"))
    assert result["plan"] == "direct"
    assert result["skill_calls"][0].skill_name == "stock_snapshot"
    assert result["skill_calls"][0].args == {"symbol": "600519"}
    assert result["goal"].symbols == ["600519"]


@pytest.mark.asyncio
async def test_resolve_symbol_news_infers_stock_news() -> None:
    """'茅台有什么新闻' → resolve 成功 → stock_news(symbol + limit)。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value="600519"),
    ):
        result = await qa_router_node(_state("茅台有什么新闻"))
    assert result["skill_calls"][0].skill_name == "stock_news"
    assert result["skill_calls"][0].args == {"symbol": "600519", "limit": 10}


@pytest.mark.asyncio
async def test_resolve_symbol_miss_falls_back_to_clarification() -> None:
    """resolve 失败 + LLM 失败 → 澄清话术，不中断、无 skill_calls。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=RuntimeError("llm down"),
    ):
        result = await qa_router_node(_state("茅台今天怎么样"))
    assert result["clarification"] == "请提供 6 位股票代码后重试。"
    assert result["skill_calls"] == []


@pytest.mark.asyncio
async def test_resolve_miss_pure_stock_question_forces_clarification() -> None:
    """首轮纯个股问句 resolve 未命中 → 强制澄清，不进 LLM（防 LLM 幻觉假代码）。"""
    mock_llm = MagicMock()
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("不存在的股票名称xyz今天怎么样"))
    mock_llm.with_structured_output.assert_not_called()
    assert result["clarification"] == "请提供 6 位股票代码后重试。"
    assert result["skill_calls"] == []


@pytest.mark.asyncio
async def test_resolve_miss_sector_intent_goes_to_llm() -> None:
    """resolve 未命中但含板块意图（"白酒板块"）→ 不澄清，放行 LLM。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="白酒板块怎么样", intent="sector_snapshot"),
        plan="direct",
        skill_calls=[SkillCall(skill_name="sector_snapshot", args={})],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("白酒板块怎么样"))
    assert "clarification" not in result
    assert result["skill_calls"][0].skill_name == "sector_snapshot"


@pytest.mark.asyncio
async def test_resolve_miss_market_intent_goes_to_llm() -> None:
    """resolve 未命中但含大盘语义（"今天A股市场整体表现怎么样"）→ 不澄清，放行 LLM。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="今天A股市场整体表现怎么样", intent="market_snapshot"),
        plan="direct",
        skill_calls=[
            SkillCall(
                skill_name="market_snapshot",
                args={"scope": "both", "snapshot_kind": "quick"},
            )
        ],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("今天A股市场整体表现怎么样"))
    assert "clarification" not in result
    assert result["skill_calls"][0].skill_name == "market_snapshot"


@pytest.mark.asyncio
async def test_resolve_miss_multiturn_goes_to_llm() -> None:
    """多轮（有历史）resolve 未命中 → 不澄清，放行 LLM 解析指代。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="它最近有什么新闻", intent="stock_news"),
        plan="direct",
        skill_calls=[
            SkillCall(skill_name="stock_news", args={"symbol": "600519", "limit": 10})
        ],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    state: QuestionState = {
        "messages": [
            HumanMessage(content="茅台现在多少钱"),
            AIMessage(content="茅台当前 1800 元"),
            HumanMessage(content="它最近有什么新闻"),
        ],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(state)
    assert "clarification" not in result
    assert result["skill_calls"][0].skill_name == "stock_news"


@pytest.mark.asyncio
async def test_postprocess_corrects_invalid_symbol() -> None:
    """LLM 输出非 6 位 symbol → resolve 纠正为 6 位。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="茅台今天怎么样", intent="stock_snapshot"),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "茅台"})],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm), patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value="600519"),
    ):
        result = await qa_router_node(_state("茅台今天怎么样"))
    assert result["skill_calls"][0].args == {"symbol": "600519"}


@pytest.mark.asyncio
async def test_postprocess_unresolvable_symbol_degrades_to_clarification() -> None:
    """LLM 输出非 6 位 symbol 且 resolve 失败 → 澄清短路。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="茅台今天怎么样", intent="stock_snapshot"),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "茅台"})],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("茅台今天怎么样"))
    assert result["clarification"] == "请提供 6 位股票代码后重试。"
    assert result["skill_calls"] == []


@pytest.mark.asyncio
async def test_postprocess_overrides_date() -> None:
    """LLM 输出 date 与消息不一致 → extract_report_date 强覆盖。"""
    from aistock_agent.graph.nodes.qa_router import extract_report_date

    message = "2026-07-31 大盘为什么涨"
    fake_output = QARouterOutput(
        goal=InsightGoal(question=message, intent="trace_lookup"),
        plan="direct",
        skill_calls=[
            SkillCall(skill_name="trace_lookup", args={"date": "2026-07-01"})
        ],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state(message))
    assert result["skill_calls"][0].args["date"] == extract_report_date(message)
    assert result["skill_calls"][0].args["date"] == "2026-07-31"


@pytest.mark.asyncio
async def test_postprocess_resolves_tag_codes() -> None:
    """LLM 输出中文 tag_codes → resolve_tag_code 转 BK 码。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(
            question="分析白酒板块",
            intent="sector_snapshot",
            tag_codes=["白酒"],
        ),
        plan="direct",
        skill_calls=[
            SkillCall(
                skill_name="sector_snapshot",
                args={"tag_codes": ["白酒", "半导体"], "tag_code": "BK9999"},
            )
        ],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("分析白酒板块"))
    assert result["skill_calls"][0].args["tag_codes"] == ["BK0477", "BK1036"]


@pytest.mark.asyncio
async def test_postprocess_unresolvable_tag_codes_removed() -> None:
    """LLM 输出中文 tag_codes 且 resolve_tag_code 未命中 → 移除参数。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="分析某个未知板块", intent="sector_snapshot"),
        plan="direct",
        skill_calls=[
            SkillCall(skill_name="sector_snapshot", args={"tag_codes": ["未知板块XYZ"]})
        ],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("分析某个未知板块"))
    assert "tag_codes" not in result["skill_calls"][0].args


# ─── D4 复杂度判定 + force_deep 入口（P1 Task 1） ───


@pytest.mark.asyncio
async def test_complexity_deep_for_stock_analysis() -> None:
    """LLM 输出 complexity=deep + stock_snapshot 分析词 → state.complexity=deep。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="300750 深度分析", intent="stock_snapshot", symbols=["300750"]),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "300750"})],
        complexity="deep",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("300750 深度分析"))
    assert result["complexity"] == "deep"
    assert result["skill_calls"][0].skill_name == "stock_snapshot"


@pytest.mark.asyncio
async def test_complexity_light_for_quote() -> None:
    """单点取数（行情/报告）→ complexity=light。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="600519 现在多少钱", intent="stock_snapshot", symbols=["600519"]),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("600519 现在多少钱"))
    assert result["complexity"] == "light"
    assert result["skill_calls"][0].skill_name == "stock_snapshot"


@pytest.mark.asyncio
async def test_complexity_fallback_rule() -> None:
    """LLM 失败 → 兜底：stock 意图 + 分析词 → deep；否则 light。"""
    cases = [
        ("600519 行情深度分析", "deep"),
        ("600519 现在多少钱", "light"),
    ]
    for message, expected in cases:
        mock_llm = MagicMock()
        mock_llm.with_structured_output = MagicMock(
            return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
        )
        with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
            result = await qa_router_node(_state(message))
        assert result["skill_calls"][0].skill_name == "stock_snapshot", message
        assert result["complexity"] == expected, message
        assert result["goal"].constraints.get("router_fallback") == "true", message


@pytest.mark.asyncio
async def test_force_deep_overrides_light() -> None:
    """force_deep=True 且 LLM 判 light → 强制 deep。"""
    fake_output = QARouterOutput(
        goal=InsightGoal(question="600519 现在多少钱", intent="stock_snapshot", symbols=["600519"]),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
        complexity="light",
    )
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=fake_output))
    )
    state = _state("600519 现在多少钱")
    state["force_deep"] = True
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(state)
    assert result["complexity"] == "deep"


@pytest.mark.asyncio
async def test_force_deep_does_not_bypass_compliance() -> None:
    """force_deep=True + 合规词（买）→ 仍合规话术短路，不 deep、LLM 不被调用。"""
    mock_llm = MagicMock()
    state = _state("茅台可以买吗")
    state["force_deep"] = True
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(state)
    mock_llm.with_structured_output.assert_not_called()
    assert result["final_response"] == COMPLIANCE_REPLY
    assert result["skill_calls"] == []
    assert result["goal"].constraints.get("guardrail") == "compliance"
    assert result["complexity"] == "light"


@pytest.mark.asyncio
async def test_complexity_missing_falls_back() -> None:
    """LLM 输出缺 complexity 字段 → ValidationError → 走既有兜底链（不崩溃）。"""

    def _raise_missing_complexity(*args, **kwargs):
        QARouterOutput.model_validate(
            {
                "goal": {"question": "600519 现在多少钱", "intent": "stock_snapshot"},
                "plan": "direct",
                "skill_calls": [{"skill_name": "stock_snapshot", "args": {"symbol": "600519"}}],
            }
        )

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_raise_missing_complexity))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("600519 现在多少钱"))
    assert result["plan"] == "direct"
    assert result["skill_calls"][0].skill_name == "stock_snapshot"
    assert result["goal"].constraints.get("router_fallback") == "true"
    assert result["complexity"] == "light"


def test_route_by_keyword_fallback_hot_burst() -> None:
    """'机构调研热门股' → 关键词兜底 skill_name=hot_burst。"""
    call = route_by_keyword_fallback("机构调研热门股")
    assert call is not None
    assert call.skill_name == "hot_burst"
    assert call.args == {}


@pytest.mark.asyncio
async def test_hot_burst_fallback_deep() -> None:
    """'机构调研热门股' → 兜底 hot_burst + complexity=deep（供 escalate）。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    )
    with patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm):
        result = await qa_router_node(_state("机构调研热门股"))
    assert result["skill_calls"][0].skill_name == "hot_burst"
    assert result["goal"].intent == "hot_burst"
    assert result["goal"].constraints.get("router_fallback") == "true"
    assert result["complexity"] == "deep"


# ─── P1 遗留问题 1：D36 停用词补全（"分析一下贵州茅台" 不再误澄清） ───


def test_extract_stock_name_candidate_removes_analysis_verbs():
    """'分析一下贵州茅台' 去词后候选应为'贵州茅台'（不残留 '分析'）。"""
    from aistock_agent.graph.nodes.qa_router import _extract_stock_name_candidate

    assert _extract_stock_name_candidate("分析一下贵州茅台") == "贵州茅台"
    assert _extract_stock_name_candidate("评价一下贵州茅台") == "贵州茅台"
    assert _extract_stock_name_candidate("解读一下贵州茅台") == "贵州茅台"


@pytest.mark.asyncio
async def test_qa_router_stock_name_with_analysis_verb_short_circuits_light(monkeypatch):
    """'分析一下贵州茅台' resolve 命中 → 闸门 2 light 快答，不再澄清。"""
    from aistock_agent.graph.nodes.qa_router import qa_router_node

    monkeypatch.setattr(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value="600519"),
    )
    result = await qa_router_node(_state("分析一下贵州茅台"))
    assert result["goal"].symbols == ["600519"]
    assert result["skill_calls"][0].skill_name == "stock_snapshot"
    assert result["complexity"] == "light"
    assert "clarification" not in result or result.get("clarification") is None


# ─── P2 Task 5：D14/D17 追问复用（last_deep_report 注入 + chat_analysis 后处理） ───


def _sample_ref() -> dict:
    """构造 DeepReportRef 形状 dict（对齐 state/chat_schema.py）。"""
    return {
        "worker": "stock",
        "report_id": "rep_1",
        "question": "深度分析一下贵州茅台",
        "summary": "贵州茅台基本面稳健，估值处于合理区间。",
        "symbols": ["600519"],
        "tag_codes": [],
        "created_at": "2026-08-02T10:00:00+08:00",
    }


def _goal(question: str) -> InsightGoal:
    return InsightGoal(
        question=question,
        intent="report_lookup",
        symbols=[],
        tag_codes=[],
        time_range="today",
    )


def _followup_output() -> QARouterOutput:
    return QARouterOutput(
        goal=_goal("刚才那个分析怎么样"),
        plan="direct",
        skill_calls=[
            SkillCall(skill_name="report_lookup", args={"report_type": "chat_analysis"})
        ],
        complexity="light",
    )


@pytest.mark.asyncio
async def test_followup_injects_last_deep_report_context(monkeypatch):
    """有 last_deep_report → LLM prompt 含摘要段（注入为追加，SYSTEM_PROMPT 常量字节不变）；
    无 → 不注入（prompt 即 SYSTEM_PROMPT 本身）。"""
    captured: dict[str, object] = {}

    async def fake_ainvoke(messages):
        captured["prompt"] = messages[0].content
        return _followup_output()

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=fake_ainvoke)
    )
    monkeypatch.setattr(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", lambda: mock_llm
    )
    # 多轮追问场景：3 条消息绕过闸门 2 澄清（resolve 失败时仅单轮强制澄清）
    base_messages = [
        HumanMessage(content="深度分析一下贵州茅台"),
        AIMessage(content="已完成深度分析。"),
        HumanMessage(content="刚才那个分析怎么样"),
    ]

    state = _state("刚才那个分析怎么样")
    state["messages"] = base_messages
    state["last_deep_report"] = _sample_ref()
    out = await qa_router_node(state)
    prompt = str(captured["prompt"])
    assert prompt.startswith(SYSTEM_PROMPT)  # 常量字节不变，摘要段为追加
    assert "上次深度分析" in prompt
    assert "report_type=chat_analysis" in prompt
    assert out["goal"].intent == "report_lookup"

    # 无 last_deep_report → 不注入
    state2 = _state("刚才那个分析怎么样")
    state2["messages"] = base_messages
    await qa_router_node(state2)
    assert captured["prompt"] == SYSTEM_PROMPT
    assert "上次深度分析" not in str(captured["prompt"])


@pytest.mark.asyncio
async def test_postprocess_injects_user_id_for_logged_in(monkeypatch):
    """登录 → report_lookup(chat_analysis) 注入 user_id，不注入 summary_fallback。"""
    out = await _postprocess_skill_calls(
        _followup_output(),
        "刚才那个分析怎么样",
        QuestionState(messages=[], user_id="u_42", last_deep_report=_sample_ref()),
    )
    args = out.skill_calls[0].args
    assert args["user_id"] == "u_42"
    assert "summary_fallback" not in args


@pytest.mark.asyncio
async def test_postprocess_uses_summary_fallback_for_anonymous(monkeypatch):
    """未登录但有 last_deep_report → 注入 summary_fallback，不注入 user_id。"""
    out = await _postprocess_skill_calls(
        _followup_output(),
        "刚才那个分析怎么样",
        QuestionState(messages=[], last_deep_report=_sample_ref()),
    )
    args = out.skill_calls[0].args
    assert args["summary_fallback"] == _sample_ref()["summary"]
    assert "user_id" not in args


@pytest.mark.asyncio
async def test_postprocess_drops_chat_analysis_without_ref(monkeypatch):
    """未登录且无 last_deep_report → 移除该 call（走既有短路/兜底）。"""
    out = await _postprocess_skill_calls(
        _followup_output(), "刚才那个分析怎么样", QuestionState(messages=[])
    )
    assert out.skill_calls == []


def test_qa_router_output_goals_default_none():
    out = QARouterOutput(
        goal=InsightGoal(question="茅台今天怎么样", intent="stock_snapshot"),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
        complexity="light",
    )
    assert out.goals is None


def test_qa_router_output_goals_parse():
    out = QARouterOutput(
        goal=InsightGoal(question="茅台明天会涨吗", intent="stock_snapshot"),
        plan="compose",
        skill_calls=[
            SkillCall(
                skill_name="stock_snapshot",
                args={"symbol": "600519"},
                goal_id="g1",
            )
        ],
        complexity="light",
        goals=[
            SubGoal(question="当前表现", intent="stock_snapshot", dimension="validate"),
            SubGoal(question="明日走势", intent="stock_snapshot", dimension="predict"),
        ],
    )
    assert [g.dimension for g in out.goals] == ["validate", "predict"]
    assert out.goals[0].id == "g1"  # 默认 id
