"""qa_router 消费 user_profile 单测（Phase 4-3 Task 4）。

覆盖（对齐 plan Task 4 §测试）：
- ① _build_user_profile_context：profile 存在 → 含称呼/投资偏好/风险偏好
- ② profile 为 None / 空 dict / 无可用字段 → 返回 ""（prompt 字节不变）
- ③ node 级：profile 注入后 LLM prompt 含偏好段
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.qa_router import (
    QARouterOutput,
    _build_user_profile_context,
    qa_router_node,
)
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState

PROFILE = {
    "user_id": "u_42",
    "nickname": "小王",
    "investment_preferences": ["白酒", "新能源"],
    "risk_tolerance": "conservative",
}


def _state(message: str, profile: dict | None = None) -> QuestionState:
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
        "user_id": "u_42",
        "user_profile": profile,
    }


# ── _build_user_profile_context 纯函数 ──


def test_profile_context_with_full_profile() -> None:
    ctx = _build_user_profile_context(PROFILE)
    assert "小王" in ctx
    assert "白酒" in ctx
    assert "conservative" in ctx
    assert ctx.startswith("\n\n")


def test_profile_context_none_returns_empty() -> None:
    assert _build_user_profile_context(None) == ""


def test_profile_context_empty_dict_returns_empty() -> None:
    assert _build_user_profile_context({}) == ""


def test_profile_context_ignores_invalid_fields() -> None:
    """非法字段类型（非 str 偏好、未知 risk_tolerance）不产生内容，返回 ""。"""
    ctx = _build_user_profile_context(
        {"nickname": "", "investment_preferences": [1, None], "risk_tolerance": "insane"}
    )
    assert ctx == ""


def test_profile_context_partial_nickname_only() -> None:
    ctx = _build_user_profile_context({"nickname": "老王"})
    assert "老王" in ctx
    assert "投资偏好" not in ctx


# ── node 级：profile 注入 LLM prompt ──


def _capturing_llm(captured: dict) -> MagicMock:
    fake_output = QARouterOutput(
        goal=InsightGoal(
            question="600519 现在多少钱", intent="stock_snapshot", symbols=["600519"]
        ),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
        complexity="light",
    )

    async def _ainvoke(messages: list) -> QARouterOutput:
        captured["prompt"] = messages[0].content
        return fake_output

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_ainvoke))
    )
    return mock_llm


@pytest.mark.asyncio
async def test_qa_router_prompt_includes_profile_context() -> None:
    """profile 存在 → LLM prompt 追加用户画像参考段。"""
    captured: dict = {}
    mock_llm = _capturing_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ):
        result = await qa_router_node(_state("600519 现在多少钱", profile=PROFILE))
    assert result["plan"] == "direct"
    assert "用户画像参考" in captured["prompt"]
    assert "小王" in captured["prompt"]


@pytest.mark.asyncio
async def test_qa_router_prompt_unchanged_without_profile() -> None:
    """profile 为 None → prompt 不含用户画像参考段（字节不变）。"""
    captured: dict = {}
    mock_llm = _capturing_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ):
        result = await qa_router_node(_state("600519 现在多少钱"))
    assert result["plan"] == "direct"
    assert "用户画像参考" not in captured["prompt"]
