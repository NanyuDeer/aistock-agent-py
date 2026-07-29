"""skill_executor 节点单元测试。"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState


def _evidence(skill: str, degraded: bool = False) -> Evidence:
    return Evidence(
        facts=[f"{skill} fact"],
        sources=[],
        as_of=datetime.now(timezone.utc),
        degraded=degraded,
        skill_name=skill,
    )


@pytest.mark.asyncio
async def test_skill_executor_single_direct():
    """direct 计划（单 Skill）正常执行。"""
    from aistock_agent.graph.nodes.skill_executor import skill_executor_node

    state: QuestionState = {
        "messages": [],
        "goal": InsightGoal(question="x", intent="report_lookup"),
        "plan": "direct",
        "skill_calls": [SkillCall(skill_name="report_lookup", args={"report_type": "review", "date": "2026-07-28"})],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }
    with patch(
        "aistock_agent.graph.nodes.skill_executor.SKILL_REGISTRY",
        {"report_lookup": AsyncMock(return_value=_evidence("report_lookup"))},
    ):
        result = await skill_executor_node(state)
    assert len(result["evidences"]) == 1
    assert result["evidences"][0].skill_name == "report_lookup"


@pytest.mark.asyncio
async def test_skill_executor_compose_parallel():
    """compose 计划（2 个无依赖 Skill）并行执行。"""
    from aistock_agent.graph.nodes.skill_executor import skill_executor_node

    state: QuestionState = {
        "messages": [],
        "goal": InsightGoal(question="x", intent="stock_snapshot"),
        "plan": "compose",
        "skill_calls": [
            SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"}),
            SkillCall(skill_name="stock_news", args={"symbol": "600519"}),
        ],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }
    with patch(
        "aistock_agent.graph.nodes.skill_executor.SKILL_REGISTRY",
        {
            "stock_snapshot": AsyncMock(return_value=_evidence("stock_snapshot")),
            "stock_news": AsyncMock(return_value=_evidence("stock_news")),
        },
    ):
        result = await skill_executor_node(state)
    assert len(result["evidences"]) == 2
    skill_names = {ev.skill_name for ev in result["evidences"]}
    assert skill_names == {"stock_snapshot", "stock_news"}


@pytest.mark.asyncio
async def test_skill_executor_skill_exception_isolated():
    """单个 Skill 异常被 @skill 装饰器捕获为 degraded，不阻断其他 Skill。"""
    from aistock_agent.graph.nodes.skill_executor import skill_executor_node

    state: QuestionState = {
        "messages": [],
        "goal": InsightGoal(question="x", intent="stock_snapshot"),
        "plan": "compose",
        "skill_calls": [
            SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"}),
            SkillCall(skill_name="stock_news", args={"symbol": "600519"}),
        ],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }

    async def boom(*args, **kwargs):
        raise RuntimeError("boom")

    with patch(
        "aistock_agent.graph.nodes.skill_executor.SKILL_REGISTRY",
        {
            "stock_snapshot": boom,  # 裸函数，绕过 @skill 装饰器以模拟 Skill 内部异常未捕获
            "stock_news": AsyncMock(return_value=_evidence("stock_news")),
        },
    ):
        # skill_executor 内部应用 @skill 包装，异常被捕获为 degraded
        result = await skill_executor_node(state)
    assert len(result["evidences"]) == 2
    degraded_evs = [ev for ev in result["evidences"] if ev.degraded]
    assert len(degraded_evs) == 1
    assert degraded_evs[0].skill_name == "stock_snapshot"
