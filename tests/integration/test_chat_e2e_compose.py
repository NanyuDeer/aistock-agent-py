# tests/integration/test_chat_e2e_compose.py
"""CHAT QA 链路端到端测试 — compose 多意图组合路径。

覆盖：
1. 多 Skill 并行（无 depends_on）
2. 多 Skill 串行（有 depends_on）
3. 部分降级（1 个 Skill 异常，其余正常，Evidence 汇总含 degraded）
"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

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


def _evidence(skill: str, facts: list[str], symbols: list[str] | None = None) -> Evidence:
    return Evidence(
        facts=facts,
        sources=[],
        as_of=datetime.now(timezone.utc),
        skill_name=skill,
        symbols=symbols or [],
    )


def _build_state(message: str) -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": None,
        "plan": "compose",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }


def _mock_llm_with_outputs(qa_output: QARouterOutput, synth_output: SynthOutput) -> MagicMock:
    """构造同时服务 qa_router 与 synth_answer 的 mock LLM。

    qa_router 调 with_structured_output(QARouterOutput).ainvoke → 第 1 个
    synth_answer 调 with_structured_output(SynthOutput).ainvoke → 第 2 个
    """
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_output)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_output)),
        ]
    )
    return mock_llm


@pytest.mark.asyncio
async def test_compose_parallel_two_skills():
    """compose 并行：stock_snapshot + industry_relation 同时执行，Evidence 汇总。"""
    qa_output = QARouterOutput(
        goal=InsightGoal(
            question="茅台现在怎么样 + 白酒板块上下游",
            intent="stock_snapshot",  # compose 时 intent 取主意图
            symbols=["600519"],
            tag_codes=["baijiu"],
            time_range="realtime",  # 实时行情 → 推断为 trace 模式
        ),
        plan="compose",
        skill_calls=[
            SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"}),
            SkillCall(skill_name="industry_relation", args={"keywords": ["白酒"]}),
        ],
    )
    synth_output = SynthOutput(
        insight=Insight(
            conclusion="茅台当前 1800 元，白酒板块上下游为食品饮料",
            basis=[
                _evidence("stock_snapshot", ["1800 元"], ["600519"]),
                _evidence("industry_relation", ["食品饮料"]),
            ],
            confidence="medium",
            uncertainty=[],
            answer_mode="trace",
        )
    )
    mock_llm = _mock_llm_with_outputs(qa_output, synth_output)

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(return_value="600519 当前价 1800"),
    ), patch(
        "aistock_agent.skills.industry_relation.match_industry_by_keywords",
        new=AsyncMock(return_value="白酒 → 食品饮料"),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_build_state("茅台现在怎么样 + 白酒板块上下游"))

    # 两个 Skill 都执行
    assert len(result["evidences"]) == 2
    skill_names = {ev.skill_name for ev in result["evidences"]}
    assert skill_names == {"stock_snapshot", "industry_relation"}
    # 无降级
    assert all(not ev.degraded for ev in result["evidences"])
    # synth 产出 Insight
    assert result["insight"] is not None
    assert result["trace"].plan == "compose"
    assert result["trace"].actual_mode == "trace"


@pytest.mark.asyncio
async def test_compose_serial_with_depends_on():
    """compose 串行：stock_news depends_on stock_snapshot，前置 symbols 供后置使用。"""
    qa_output = QARouterOutput(
        goal=InsightGoal(
            question="茅台行情和新闻",
            intent="stock_snapshot",
            symbols=["600519"],
        ),
        plan="compose",
        skill_calls=[
            SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"}),
            SkillCall(
                skill_name="stock_news",
                args={"limit": 10},
                depends_on=["stock_snapshot"],
            ),
        ],
    )
    synth_output = SynthOutput(
        insight=Insight(
            conclusion="茅台 1800 元，近期发布半年报",
            basis=[
                _evidence("stock_snapshot", ["1800 元"], ["600519"]),
                _evidence("stock_news", ["半年报"], ["600519"]),
            ],
            confidence="medium",
            uncertainty=[],
            answer_mode="trace",
        )
    )
    mock_llm = _mock_llm_with_outputs(qa_output, synth_output)

    # stock_news 的 args 不含 symbol，应从 goal.symbols 或前置 Evidence.symbols 取
    captured_news_args: dict = {}

    async def fake_stock_news(args, goal):
        captured_news_args.update(args)
        return _evidence("stock_news", ["茅台发布半年报"], ["600519"])

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(return_value="600519 当前价 1800"),
    ), patch(
        "aistock_agent.graph.nodes.skill_executor.SKILL_REGISTRY",
        {
            "stock_snapshot": AsyncMock(return_value=_evidence("stock_snapshot", ["1800 元"], ["600519"])),
            "stock_news": fake_stock_news,
        },
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_build_state("茅台行情和新闻"))

    # 两个 Skill 都执行
    assert len(result["evidences"]) == 2
    # 串行执行顺序：stock_snapshot 在前，stock_news 在后
    assert result["evidences"][0].skill_name == "stock_snapshot"
    assert result["evidences"][1].skill_name == "stock_news"


@pytest.mark.asyncio
async def test_compose_partial_degraded():
    """compose 部分降级：3 个 Skill，stock_news 异常，其余正常，Evidence 汇总含 degraded。"""
    qa_output = QARouterOutput(
        goal=InsightGoal(
            question="茅台行情+新闻+板块",
            intent="stock_snapshot",
            symbols=["600519"],
            tag_codes=["baijiu"],
        ),
        plan="compose",
        skill_calls=[
            SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"}),
            SkillCall(skill_name="stock_news", args={"symbol": "600519"}),
            SkillCall(skill_name="industry_relation", args={"keywords": ["白酒"]}),
        ],
    )
    # synth 应选 validate 模式（因有 degraded Evidence）
    synth_output = SynthOutput(
        insight=Insight(
            conclusion="茅台 1800 元，板块为食品饮料，新闻暂不可用",
            basis=[
                _evidence("stock_snapshot", ["1800 元"], ["600519"]),
                _evidence("industry_relation", ["食品饮料"]),
            ],
            confidence="low",
            uncertainty=["stock_news 工具失败"],
            answer_mode="validate",
        )
    )
    mock_llm = _mock_llm_with_outputs(qa_output, synth_output)

    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ), patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(return_value="600519 当前价 1800"),
    ), patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=AsyncMock(side_effect=RuntimeError("cls api down")),
    ), patch(
        "aistock_agent.skills.industry_relation.match_industry_by_keywords",
        new=AsyncMock(return_value="白酒 → 食品饮料"),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_build_state("茅台行情+新闻+板块"))

    # 3 个 Skill 都执行，1 个降级
    assert len(result["evidences"]) == 3
    degraded_evs = [ev for ev in result["evidences"] if ev.degraded]
    assert len(degraded_evs) == 1
    assert degraded_evs[0].skill_name == "stock_news"
    # 正常 Skill 不受影响
    ok_evs = [ev for ev in result["evidences"] if not ev.degraded]
    assert {ev.skill_name for ev in ok_evs} == {"stock_snapshot", "industry_relation"}
    # synth 降级为 validate（因有 degraded Evidence）
    assert result["trace"].actual_mode == "validate"
