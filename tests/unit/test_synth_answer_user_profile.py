"""synth_answer 消费 user_profile 单测（Phase 4-3 Task 4）。

覆盖（对齐 plan Task 4 §测试）：
- ① 风险段按 risk_tolerance=conservative 强化（含"风险较高，谨慎对待"）
- ② profile 为 None / risk_tolerance 缺失 → 风险段字节不变（常规档）
- ③ LLM 成功路径：conservative 档生效；无 profile 常规档
- ④ 多子目标按 investment_preferences 重排（偏好命中前置，不改 evidence 关联）
"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.synth_answer import (
    RISK_DISCLAIMER_CONSERVATIVE,
    SynthOutput,
    _append_risk_disclaimer,
    _sort_goals_by_preferences,
    _synth_multi_goal,
    synth_answer_node,
)
from aistock_agent.prompts.general.system import RISK_DISCLAIMER, RISK_DISCLAIMER_STRONG
from aistock_agent.schemas.chat_contract import (
    ChatSource,
    Evidence,
    InsightGoal,
    SubGoal,
)
from aistock_agent.state.chat_schema import QuestionState

PROFILE_CONSERVATIVE = {
    "user_id": "u_42",
    "nickname": "小王",
    "investment_preferences": ["白酒", "新能源"],
    "risk_tolerance": "conservative",
}


def _state(profile: dict | None = None) -> QuestionState:
    return {
        "messages": [HumanMessage(content="600519 现在多少钱")],
        "goal": InsightGoal(question="600519 现在多少钱", intent="stock_snapshot"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "user_profile": profile,
    }


def _mock_synth_llm(conclusion: str) -> MagicMock:
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(
            ainvoke=AsyncMock(
                return_value=SynthOutput.model_validate(
                    {
                        "insight": {
                            "conclusion": conclusion,
                            "basis_indices": [],
                            "confidence": "low",
                            "answer_mode": "validate",
                        }
                    }
                )
            )
        )
    )
    return mock_llm


# ── ① 风险段档位（纯函数） ──


def test_risk_disclaimer_conservative_tier() -> None:
    out = _append_risk_disclaimer("正文", risk_tolerance="conservative")
    assert RISK_DISCLAIMER_CONSERVATIVE in out
    assert "正文" in out


def test_risk_disclaimer_conservative_precedes_strong() -> None:
    """conservative 优先于动作词 strong 档（双触发时取 conservative 强化档）。"""
    out = _append_risk_disclaimer(
        "正文", strong=True, risk_tolerance="conservative"
    )
    assert RISK_DISCLAIMER_CONSERVATIVE in out
    assert RISK_DISCLAIMER_STRONG not in out


def test_risk_disclaimer_no_profile_uses_default() -> None:
    out = _append_risk_disclaimer("正文")
    assert RISK_DISCLAIMER in out
    assert RISK_DISCLAIMER_CONSERVATIVE not in out


def test_risk_disclaimer_strong_without_profile() -> None:
    out = _append_risk_disclaimer("正文", strong=True)
    assert RISK_DISCLAIMER_STRONG in out
    assert RISK_DISCLAIMER_CONSERVATIVE not in out


def test_risk_disclaimer_conservative_dedup() -> None:
    """已含 conservative 档则不重复叠加。"""
    text = f"正文\n\n{RISK_DISCLAIMER_CONSERVATIVE}"
    assert _append_risk_disclaimer(text, risk_tolerance="conservative") == text


# ── ②/③ LLM 成功路径风险段档位 ──


@pytest.mark.asyncio
async def test_synth_answer_conservative_risk_tier_applied() -> None:
    """profile risk_tolerance=conservative → 结论含 conservative 强化档（互斥取代常规档）。"""
    mock_llm = _mock_synth_llm("贵州茅台今日震荡上行。")
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        result = await synth_answer_node(_state(profile=PROFILE_CONSERVATIVE))
    assert RISK_DISCLAIMER_CONSERVATIVE in result["final_response"]
    assert RISK_DISCLAIMER not in result["final_response"]


@pytest.mark.asyncio
async def test_synth_answer_no_profile_risk_tier_unchanged() -> None:
    """无 profile → 风险段维持常规档（字节不变）。"""
    mock_llm = _mock_synth_llm("贵州茅台今日震荡上行。")
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
    ):
        result = await synth_answer_node(_state())
    assert RISK_DISCLAIMER in result["final_response"]
    assert RISK_DISCLAIMER_CONSERVATIVE not in result["final_response"]


# ── ④ 多子目标排序（纯函数） ──


def _subgoal(question: str) -> SubGoal:
    return SubGoal(
        id=f"g_{len(question)}",
        question=question,
        intent="stock_snapshot",
        dimension="validate",
        symbols=[],
        tag_codes=[],
        time_range="today",
    )


def test_sort_goals_by_preferences_moves_match_first() -> None:
    """偏好命中子目标前置，未命中保持原相对顺序。"""
    news = _subgoal("新能源板块最近新闻")
    baijiu = _subgoal("白酒板块行情")
    market = _subgoal("大盘怎么样")
    ordered = _sort_goals_by_preferences([news, baijiu, market], ["白酒"])
    assert ordered == [baijiu, news, market]


def test_sort_goals_by_preferences_none_keeps_order() -> None:
    goals = [_subgoal("a 板块"), _subgoal("b 板块")]
    assert _sort_goals_by_preferences(goals, None) == goals
    assert _sort_goals_by_preferences(goals, []) == goals


def test_sort_goals_by_preferences_ignores_invalid() -> None:
    goals = [_subgoal("白酒行情"), _subgoal("新能源行情")]
    assert _sort_goals_by_preferences(goals, [1, None, ""]) == goals


# ── 多节路径：multi_goal 排序接入（evidence 关联不变） ──


def _evidence(facts: list[str], goal_id: str) -> Evidence:
    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id="s1",
                kind="realtime_quote",
                title="t",
                url="u",
                snippet="s",
                captured_at=datetime.now(UTC),
            )
        ],
        as_of=datetime.now(UTC),
        skill_name="stock_snapshot",
        goal_id=goal_id,
    )


@pytest.mark.asyncio
async def test_synth_multi_goal_preference_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_synth_multi_goal：偏好命中子目标 section 前置；evidence 仍按 goal_id 关联。"""
    goals = [
        _subgoal("新能源板块最近新闻"),
        _subgoal("白酒板块行情"),
    ]
    evs = [
        _evidence(["新能源事实"], "g_10"),
        _evidence(["白酒事实"], "g_8"),
    ]

    async def _fake_section(
        goal: InsightGoal, evidences: list[Evidence], summary_context: str = ""
    ) -> object:
        from aistock_agent.graph.nodes.synth_answer import _SectionResult

        return _SectionResult(
            conclusion=goal.question,
            basis=evidences,
            uncertainty=[],
            degraded=False,
            mode="validate",
            confidence="low",
        )

    monkeypatch.setattr(
        "aistock_agent.graph.nodes.synth_answer._synth_section", _fake_section
    )
    state = _state(profile={"risk_tolerance": "balanced", "investment_preferences": ["白酒"]})
    state["goals"] = goals
    state["plan"] = "compose"
    state["skill_calls"] = []
    state["evidences"] = evs
    result = await _synth_multi_goal(
        state, InsightGoal(question="x", intent="market_snapshot"), evs, goals
    )
    combined = result["final_response"]
    assert combined.index("白酒板块行情") < combined.index("新能源板块最近新闻")
