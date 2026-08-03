"""CHAT QA 链路降级测试 — 三层降级链路。"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.state.chat_schema import QuestionState


@pytest.mark.asyncio
async def test_degraded_skill_exception_recovers():
    """第 1 层：Skill 异常 → degraded Evidence，链路继续，synth 降级 validate。"""
    # QA Router 成功路由
    from aistock_agent.graph.nodes.qa_router import QARouterOutput
    from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall

    qa_output = QARouterOutput(
        goal=InsightGoal(question="茅台现在多少钱", intent="stock_snapshot", symbols=["600519"]),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
        complexity="light",
    )

    # synth 也成功返回（但应选 validate 模式因 Evidence degraded）
    from aistock_agent.graph.nodes.synth_answer import SynthInsightOutput, SynthOutput

    synth_output = SynthOutput(
        insight=SynthInsightOutput(
            conclusion="行情数据暂不可用",
            basis_indices=[],
            confidence="low",
            uncertainty=["行情工具失败"],
            answer_mode="validate",  # 应被推断为 validate
        )
    )

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_output)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_output)),
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
        new=AsyncMock(side_effect=RuntimeError("network timeout")),
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

    # Skill 异常被 @skill 捕获为 degraded Evidence
    assert len(result["evidences"]) == 1
    assert result["evidences"][0].degraded is True
    # synth 仍执行，Insight 产出
    assert result["insight"] is not None
    # 模式应被推断为 validate（因 Evidence degraded）
    assert result["trace"].actual_mode == "validate"


@pytest.mark.asyncio
async def test_degraded_qa_router_fallback():
    """第 1 层（QA Router 侧）：LLM 失败 → 关键词兜底。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    )

    # synth 成功返回
    from aistock_agent.graph.nodes.synth_answer import SynthInsightOutput, SynthOutput

    synth_output = SynthOutput(
        insight=SynthInsightOutput(
            conclusion="今日晨报内容",
            basis_indices=[],
            confidence="medium",
            uncertainty=[],
            answer_mode="validate",
        )
    )
    mock_deep = MagicMock()
    mock_deep.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=synth_output))
    )

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_deep
    ), patch(
        "aistock_agent.skills.report_lookup.get_cached_review",
        new=AsyncMock(return_value={"markdown": "晨报", "trace_summary": "震荡"}),
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

    # 关键词兜底到 report_lookup
    assert result["goal"].constraints.get("router_fallback") == "true"
    assert result["skill_calls"][0].skill_name == "report_lookup"


@pytest.mark.asyncio
async def test_clarification_when_stock_symbol_missing():
    """澄清路径：quick LLM 失败 + 个股意图缺 6 位代码 → 写澄清，无 skill 调用、无二次 deep LLM。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("llm down")))
    )

    # 澄清路径不应触发 deep LLM：若误触发，AssertionError 使最终断言失败
    mock_deep = MagicMock()
    mock_deep.with_structured_output = MagicMock(
        return_value=MagicMock(
            ainvoke=AsyncMock(
                side_effect=AssertionError("clarification path must not call deep_think")
            )
        )
    )

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_deep
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
            "clarification": None,
        }
        result = await graph.ainvoke(state)

    assert result["skill_calls"] == []
    assert result["evidences"] == []
    assert result["final_response"] == "请提供 6 位股票代码后重试。"


@pytest.mark.asyncio
async def test_degraded_synth_failure_returns_low_confidence():
    """第 2 层：synth_answer LLM 失败 → 降级 validate + 拼接 facts + low。"""
    from aistock_agent.graph.nodes.qa_router import QARouterOutput
    from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall

    qa_output = QARouterOutput(
        goal=InsightGoal(question="今天晨报", intent="report_lookup"),
        plan="direct",
        skill_calls=[
            SkillCall(
                skill_name="report_lookup",
                args={"report_type": "review", "date": "2026-07-28"},
            )
        ],
        complexity="light",
    )

    mock_quick = MagicMock()
    mock_quick.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=qa_output))
    )

    mock_deep = MagicMock()
    mock_deep.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=RuntimeError("deep_think down")))
    )

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_quick
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_deep
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

    # synth 降级
    assert result["insight"] is not None
    assert result["insight"].confidence == "low"
    assert result["insight"].answer_mode == "validate"
    assert len(result["insight"].uncertainty) >= 1
    assert "综合失败" in result["insight"].uncertainty[0]


@pytest.mark.asyncio
async def test_synth_answer_out_of_range_basis_indices_degrades_structured() -> None:
    """LLM 输出越界 basis_indices → 安全降级 + 分节结构降级文案。"""
    from datetime import UTC, datetime

    from aistock_agent.graph.nodes.synth_answer import (
        SynthInsightOutput,
        SynthOutput,
        synth_answer_node,
    )
    from aistock_agent.schemas.chat_contract import Evidence, InsightGoal

    goal = InsightGoal(question="宁德时代有什么最新新闻", intent="stock_news")
    evidence = Evidence(
        facts=["财联社7月31日电，宁德时代拟派现..."],
        sources=[],
        as_of=datetime.now(UTC),
        degraded=False,
        skill_name="stock_news",
    )
    state = {
        "messages": [HumanMessage(content="宁德时代有什么最新新闻")],
        "goal": goal,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [evidence],
        "insight": None,
        "final_response": "",
        "trace": None,
    }

    # LLM 返回越界 basis_indices=[2]（仅 1 条证据）
    fake = MagicMock()
    fake.ainvoke = AsyncMock(
        return_value=SynthOutput(
            insight=SynthInsightOutput(
                conclusion="宁德时代新闻",
                basis_indices=[2],
                confidence="low",
                uncertainty=[],
                answer_mode="trace",
            )
        )
    )

    with (
        patch("aistock_agent.graph.nodes.synth_answer.get_deep_think"),
        patch(
            "aistock_agent.graph.nodes.synth_answer.with_chat_structured_output",
            return_value=fake,
        ),
    ):
        result = await synth_answer_node(state)

    assert result["insight"].confidence == "low"
    assert result["insight"].conclusion.startswith("## 核心结论")
    assert "宁德" in result["insight"].conclusion
