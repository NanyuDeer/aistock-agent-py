"""qa_router 消费 user_profile / user_profile_inferences 单测。

覆盖（对齐 plan Task 4 / Task 7 §测试）：
- ① _build_user_profile_context：profile 存在 → 含称呼/投资偏好/风险偏好
- ② profile 为 None / 空 dict / 无可用字段 → 返回 ""（prompt 字节不变）
- ③ node 级：profile 注入后 LLM prompt 含偏好段
- ④ _build_inference_context：阈值过滤 / 显式优先 / 维度白名单 / ≤300 字 / 推测标注
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.qa_router import (
    QARouterOutput,
    _build_inference_context,
    _build_user_profile_context,
    qa_router_node,
)
from aistock_agent.prompts.general.system import COMPLIANCE_REPLY
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState

PROFILE = {
    "user_id": "u_42",
    "nickname": "小王",
    "investment_preferences": ["白酒", "新能源"],
    "risk_tolerance": "conservative",
}

INFERENCES = [
    {
        "dimension": "preferred_sectors",
        "value": {
            "items": [
                {"name": "白酒", "stock_count": 3, "weight": 0.19},
                {"name": "新能源", "stock_count": 2, "weight": 0.23},
            ],
            "total": 16,
        },
        "confidence": 0.70,
        "sample_size": 16,
        "window_days": None,
        "inferred_at": "2026-11-05T00:30:00Z",
    },
    {
        "dimension": "question_topics",
        "value": {
            "items": [{"topic": "财报", "hits": 12}, {"topic": "板块轮动", "hits": 9}],
            "total": 40,
        },
        "confidence": 0.65,
        "sample_size": 40,
        "window_days": 30,
        "inferred_at": "2026-11-05T00:30:00Z",
    },
    {
        "dimension": "watch_events",
        "value": {
            "items": [
                {"type": "limit_up_radar", "count": 9},
                {"type": "up_move", "count": 3},
            ],
            "total": 12,
        },
        "confidence": 0.60,
        "sample_size": 12,
        "window_days": 30,
        "inferred_at": "2026-11-05T00:30:00Z",
    },
    {
        "dimension": "active_hours",
        "value": {"items": [{"hour": 21, "hits": 18}], "total": 60},
        "confidence": 0.60,
        "sample_size": 60,
        "window_days": 30,
        "inferred_at": "2026-11-05T00:30:00Z",
    },
    {
        "dimension": "watchlist_breadth",
        "value": {"band": "5-15", "count": 9},
        "confidence": 0.70,
        "sample_size": 9,
        "window_days": None,
        "inferred_at": "2026-11-05T00:30:00Z",
    },
]


def _state(
    message: str,
    profile: dict | None = None,
    inferences: list[dict] | None = None,
) -> QuestionState:
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
        "user_profile_inferences": inferences,
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


# ── _build_inference_context 纯函数（用户画像自动挖掘一期 Task 7） ──


def test_inference_context_renders_prompt_dimensions() -> None:
    """高置信推断 → 三个进 prompt 的维度各渲染一句自然语言，且标注「推测」。"""
    ctx = _build_inference_context(INFERENCES, None)
    assert ctx.startswith("\n\n")
    assert "推测" in ctx
    assert "用户自选股集中于白酒、新能源" in ctx
    assert "42%" in ctx  # 0.19 + 0.23
    assert "财报" in ctx
    assert "涨停雷达" in ctx
    assert len(ctx) <= 300


def test_inference_context_filters_low_confidence() -> None:
    """confidence < 0.55 的维度整体丢弃；全部低置信 → 返回 ""。"""
    low = [
        {**INFERENCES[0], "confidence": 0.54},
        {**INFERENCES[1], "confidence": 0.40},
        {**INFERENCES[2], "confidence": 0.45},
    ]
    assert _build_inference_context(low, None) == ""
    # 边界：恰好 0.55 保留
    assert "财报" in _build_inference_context([{**INFERENCES[1], "confidence": 0.55}], None)


def test_inference_context_tolerates_string_confidence() -> None:
    """app-api 契约为 number，但字符串形式需 float() 容错；非法字符串丢弃。"""
    ctx = _build_inference_context([{**INFERENCES[1], "confidence": "0.62"}], None)
    assert "财报" in ctx
    assert _build_inference_context([{**INFERENCES[1], "confidence": "abc"}], None) == ""
    assert _build_inference_context([{**INFERENCES[1], "confidence": None}], None) == ""


def test_inference_context_explicit_preferences_drop_preferred_sectors() -> None:
    """显式 investment_preferences 非空 → preferred_sectors 推断整体丢弃（显式优先）。"""
    ctx = _build_inference_context(INFERENCES, {"investment_preferences": ["白酒"]})
    assert "用户自选股集中于" not in ctx
    assert "财报" in ctx  # 其他维度不受影响


def test_inference_context_blank_preferences_do_not_drop_sectors() -> None:
    """显式偏好为空白/非法 → 视为未填，preferred_sectors 仍渲染。"""
    ctx = _build_inference_context([INFERENCES[0]], {"investment_preferences": ["  ", 1]})
    assert "用户自选股集中于" in ctx


def test_inference_context_risk_tolerance_does_not_add_risk_text() -> None:
    """risk_tolerance 非空：推断段不出现风险偏好措辞（不改写风险段强度）。"""
    ctx = _build_inference_context(INFERENCES, {"risk_tolerance": "conservative"})
    assert "用户自选股集中于" in ctx
    assert "风险偏好" not in ctx
    assert "conservative" not in ctx
    assert "aggressive" not in ctx


def test_inference_context_none_and_empty_return_empty() -> None:
    """None / [] 两态 → 返回 ""（prompt 字节不变）。"""
    assert _build_inference_context(None, PROFILE) == ""
    assert _build_inference_context([], PROFILE) == ""


def test_inference_context_skips_non_prompt_dimensions() -> None:
    """active_hours / watchlist_breadth 不进 prompt（即使数据存在、置信度达标）。"""
    rows = [INFERENCES[3], INFERENCES[4]]
    assert _build_inference_context(rows, None) == ""


def test_inference_context_render_length_capped_at_300() -> None:
    """超长维度值 → 拼接文本仍 ≤ 300 字，且「推测」标注保留在段首。"""
    long_name = "超长板块名称" * 20
    rows = [
        {**INFERENCES[0], "value": {"items": [{"name": long_name, "weight": 0.2}], "total": 5}},
        {
            **INFERENCES[1],
            "value": {"items": [{"topic": long_name, "hits": 3}], "total": 3},
        },
        {
            **INFERENCES[2],
            "value": {"items": [{"type": "limit_up_radar", "count": 3}], "total": 3},
        },
    ]
    ctx = _build_inference_context(rows, None)
    assert len(ctx) <= 300
    assert "推测" in ctx


def test_inference_context_ignores_malformed_rows() -> None:
    """脏行（非 dict / 缺 dimension / value 非 dict / 空 items）静默跳过，不上抛。"""
    rows = [
        {"dimension": "question_topics", "value": "bad", "confidence": 0.9},
        {"dimension": "not_a_dimension", "value": {"items": []}, "confidence": 0.9},
        {"value": {"items": []}, "confidence": 0.9},
        {"dimension": "question_topics", "value": {"items": []}, "confidence": 0.9},
    ]
    assert _build_inference_context(rows, None) == ""  # type: ignore[arg-type]


# ── node 级：推断注入 LLM prompt ──


@pytest.mark.asyncio
async def test_qa_router_prompt_includes_inference_context() -> None:
    """注入 user_profile_inferences → LLM prompt 追加推断参考段（含「推测」）。"""
    captured: dict = {}
    mock_llm = _capturing_llm(captured)
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
    ):
        result = await qa_router_node(
            _state("600519 现在多少钱", inferences=INFERENCES)
        )
    assert result["plan"] == "direct"
    assert "用户画像推断参考" in captured["prompt"]
    assert "推测" in captured["prompt"]
    assert "用户自选股集中于白酒、新能源" in captured["prompt"]


@pytest.mark.asyncio
async def test_qa_router_prompt_unchanged_without_inferences() -> None:
    """无推断（None / []）→ prompt 不含推断参考段（字节不变）。"""
    for rows in (None, []):
        captured: dict = {}
        mock_llm = _capturing_llm(captured)
        with patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
        ):
            await qa_router_node(_state("600519 现在多少钱", inferences=rows))
        assert "用户画像推断参考" not in captured["prompt"]
        assert "推测" not in captured["prompt"]


@pytest.mark.asyncio
async def test_compliance_gate_priority_unaffected_by_inferences() -> None:
    """合规闸门优先级不受推断影响：命中敏感词仍零 LLM、直接合规话术。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.get_quick_think",
        side_effect=AssertionError("LLM should not be called on compliance gate"),
    ):
        result = await qa_router_node(_state("我能买茅台吗", inferences=INFERENCES))
    assert result["final_response"] == COMPLIANCE_REPLY
    assert result["skill_calls"] == []
    assert result["goal"].constraints.get("guardrail") == "compliance"
