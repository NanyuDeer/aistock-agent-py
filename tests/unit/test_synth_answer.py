"""synth_answer 节点单元测试。"""
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from aistock_agent.graph.nodes.synth_answer import (
    SynthOutput,
    _build_prompt,
    _resolve_basis_indices,
    synth_answer_node,
)
from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.state.chat_schema import QuestionState

CLARIFICATION = "请提供 6 位股票代码后重试。"


def _evidence(skill: str, facts: list[str]) -> Evidence:
    return Evidence(
        facts=facts,
        sources=[],
        as_of=datetime.now(UTC),
        skill_name=skill,
    )


def _state_with_clarification(message: str = "茅台最近新闻") -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": InsightGoal(question=message, intent="stock_news"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "clarification": CLARIFICATION,
    }


def _state(message: str = "茅台现在多少钱") -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": InsightGoal(question=message, intent="stock_snapshot"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }


def _state_with_evidences(
    evidences: list[Evidence], message: str = "今天为什么涨"
) -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": InsightGoal(question=message, intent="trace_lookup"),
        "plan": "direct",
        "skill_calls": [],
        "evidences": evidences,
        "insight": None,
        "final_response": "",
        "trace": None,
    }


def _mock_synth_llm(insight_dict: dict) -> MagicMock:
    """构造返回指定 SynthOutput 的 mock LLM。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(
            ainvoke=AsyncMock(
                return_value=SynthOutput.model_validate({"insight": insight_dict})
            )
        )
    )
    return mock_llm


@pytest.mark.asyncio
async def test_synth_answer_clarification_short_circuits() -> None:
    """澄清路径短路：不触发 deep LLM，返回低置信度澄清响应。"""
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("deep LLM should not be called on clarification path"),
    ):
        result = await synth_answer_node(_state_with_clarification())

    assert result["final_response"] == CLARIFICATION
    assert result["insight"].conclusion == CLARIFICATION
    assert result["insight"].confidence == "low"
    assert result["insight"].answer_mode == "validate"
    assert result["trace"].actual_mode == "validate"
    assert result["trace"].skill_calls == []
    assert result["messages"][0].content == CLARIFICATION


def test_synth_output_wrapped_shape_validates() -> None:
    """包装形态 {"insight": {...}} 含 basis_indices 可通过 SynthOutput 校验。"""
    output = SynthOutput.model_validate(
        {
            "insight": {
                "conclusion": "结论",
                "basis_indices": [],
                "confidence": "low",
                "uncertainty": [],
                "answer_mode": "validate",
            }
        }
    )
    assert output.insight.conclusion == "结论"
    assert output.insight.answer_mode == "validate"
    assert output.insight.basis_indices == []


def test_synth_output_rejects_bare_insight_shape() -> None:
    """裸 Insight 形态（顶层直接是 conclusion/basis_indices 等字段）必须失败。"""
    with pytest.raises(ValidationError):
        SynthOutput.model_validate(
            {
                "conclusion": "裸字段",
                "basis_indices": [],
                "confidence": "low",
                "uncertainty": [],
                "answer_mode": "validate",
            }
        )


def test_synth_output_rejects_legacy_basis_field() -> None:
    """旧 contract：LLM 重建完整 Evidence（basis 对象数组 / skill/reason）必须被拒绝。"""
    with pytest.raises(ValidationError):
        SynthOutput.model_validate(
            {
                "insight": {
                    "conclusion": "结论",
                    "basis": [{"skill": "trace_lookup", "reason": "x"}],
                    "confidence": "low",
                    "uncertainty": [],
                    "answer_mode": "validate",
                }
            }
        )


@pytest.mark.parametrize(
    "insight_dict",
    [
        # 缺失 basis_indices（P1：必填字段缺失）
        {"conclusion": "结论", "confidence": "low", "uncertainty": [], "answer_mode": "validate"},
        # 字符串数字（lax int 会强制转换，必须拒绝）
        {"conclusion": "结论", "basis_indices": ["1"], "confidence": "low",
         "uncertainty": [], "answer_mode": "validate"},
        # 浮点数
        {"conclusion": "结论", "basis_indices": [1.0], "confidence": "low",
         "uncertainty": [], "answer_mode": "validate"},
        # 布尔
        {"conclusion": "结论", "basis_indices": [True], "confidence": "low",
         "uncertainty": [], "answer_mode": "validate"},
    ],
)
def test_synth_output_rejects_non_strict_basis_indices(insight_dict: dict) -> None:
    """缺失 / 字符串 / 浮点 / 布尔 basis_indices 必须触发 ValidationError（严格整数契约）。"""
    with pytest.raises(ValidationError):
        SynthOutput.model_validate({"insight": insight_dict})


def test_build_prompt_declares_basis_indices_contract() -> None:
    """Prompt 声明 basis_indices（1 基序号）契约并禁止重建完整证据。"""
    goal = InsightGoal(question="茅台现在多少钱", intent="stock_snapshot")
    prompt = _build_prompt(goal, [], "validate")
    assert '"insight"' in prompt
    assert '"basis_indices"' in prompt
    assert "1 基" in prompt
    assert "禁止输出完整" in prompt


def test_resolve_basis_indices_maps_one_based() -> None:
    """1 基序号映射到 state.evidences，返回服务端原始 Evidence 对象。"""
    ev1 = _evidence("trace_lookup", ["事实1"])
    ev2 = _evidence("market_snapshot", ["事实2"])
    basis, error = _resolve_basis_indices([1, 2], [ev1, ev2])
    assert error is None
    assert basis == [ev1, ev2]
    assert basis[0] is ev1  # 必须是同一对象（服务端生成，非 LLM 重建）


def test_resolve_basis_indices_out_of_order_ok() -> None:
    """序号乱序也合法，按 LLM 给定顺序引用。"""
    ev1 = _evidence("trace_lookup", ["事实1"])
    ev2 = _evidence("market_snapshot", ["事实2"])
    basis, error = _resolve_basis_indices([2, 1], [ev1, ev2])
    assert error is None
    assert basis == [ev2, ev1]


def test_resolve_basis_indices_empty_evidences() -> None:
    """空证据：空序号合法返回空数组；非空序号视为越界降级。"""
    assert _resolve_basis_indices([], []) == ([], None)
    _, error = _resolve_basis_indices([1], [])
    assert error is not None


@pytest.mark.parametrize(
    "indices",
    [
        [0],
        [-1],
        [3],  # 越界（只有 2 条证据）
        [1, 1],  # 重复
    ],
)
def test_resolve_basis_indices_invalid_degrades(indices: list[int]) -> None:
    """0 / 负数 / 越界 / 重复序号必须返回错误（进入现有安全降级），不得静默替换。"""
    ev1 = _evidence("trace_lookup", ["事实1"])
    ev2 = _evidence("market_snapshot", ["事实2"])
    _, error = _resolve_basis_indices(indices, [ev1, ev2])
    assert error is not None


@pytest.mark.asyncio
async def test_synth_answer_maps_basis_indices_to_evidences() -> None:
    """LLM 返回 basis_indices=[1,2] → insight.basis 映射为 state.evidences 原对象。"""
    ev1 = _evidence("trace_lookup", ["动因1"])
    ev2 = _evidence("market_snapshot", ["动因2"])
    mock_llm = _mock_synth_llm(
        {
            "conclusion": "今日上涨由白酒带动",
            "basis_indices": [1, 2],
            "confidence": "medium",
            "uncertainty": [],
            "answer_mode": "trace",
        }
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([ev1, ev2]))

    assert result["insight"].conclusion == "今日上涨由白酒带动"
    assert result["insight"].basis == [ev1, ev2]
    assert result["insight"].basis[0] is ev1
    assert result["insight"].basis[1] is ev2
    assert result["trace"].actual_mode == "trace"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "indices",
    [
        [0],
        [-1],
        [5],  # 越界
        [1, 1],  # 重复
    ],
)
async def test_synth_answer_invalid_basis_indices_degrades(indices: list[int]) -> None:
    """非法 basis_indices 进入现有安全降级，不中断图、不静默改为全部证据。"""
    ev1 = _evidence("trace_lookup", ["动因1"])
    ev2 = _evidence("market_snapshot", ["动因2"])
    mock_llm = _mock_synth_llm(
        {
            "conclusion": "不应被采纳的结论",
            "basis_indices": indices,
            "confidence": "high",
            "uncertainty": [],
            "answer_mode": "trace",
        }
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([ev1, ev2]))

    assert result["final_response"].startswith("## 核心结论")
    assert result["insight"].confidence == "low"
    assert result["insight"].answer_mode == "validate"
    assert result["insight"].basis == [ev1, ev2]  # 降级仍引用服务端全部证据


@pytest.mark.asyncio
async def test_synth_answer_empty_evidences_empty_indices_ok() -> None:
    """空证据 + 空序号：正常产出，basis 为空数组。"""
    mock_llm = _mock_synth_llm(
        {
            "conclusion": "无证据结论",
            "basis_indices": [],
            "confidence": "low",
            "uncertainty": [],
            "answer_mode": "validate",
        }
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([]))

    assert result["insight"].conclusion == "无证据结论"
    assert result["insight"].basis == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "insight_dict",
    [
        # 缺失 basis_indices
        {"conclusion": "x", "confidence": "low", "uncertainty": [], "answer_mode": "validate"},
        # 字符串数字
        {"conclusion": "x", "basis_indices": ["1"], "confidence": "low",
         "uncertainty": [], "answer_mode": "validate"},
        # 浮点数
        {"conclusion": "x", "basis_indices": [1.0], "confidence": "low",
         "uncertainty": [], "answer_mode": "validate"},
        # 布尔
        {"conclusion": "x", "basis_indices": [True], "confidence": "low",
         "uncertainty": [], "answer_mode": "validate"},
    ],
)
async def test_synth_answer_invalid_basis_indices_type_degrades(insight_dict: dict) -> None:
    """非严格 basis_indices（LLM 输出经 DTO 校验失败）→ 节点走既有安全降级，不中断图。"""

    def _raise_parse_error(*args, **kwargs):
        # 模拟 json_mode 对非法 basis_indices 的真实校验失败
        SynthOutput.model_validate({"insight": insight_dict})

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_raise_parse_error))
    )
    ev1 = _evidence("trace_lookup", ["动因1"])
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([ev1]))

    assert result["final_response"].startswith("## 核心结论")
    assert result["insight"].confidence == "low"
    assert result["insight"].answer_mode == "validate"
    assert result["insight"].basis == [ev1]  # 降级仍引用服务端全部证据


@pytest.mark.asyncio
async def test_synth_answer_maps_full_evidence_reference_unchanged() -> None:
    """带非空 sources/as_of/raw 的 Evidence：insight.basis 引用原对象、字段未改写。"""
    captured_at = datetime.now(UTC)
    ev = Evidence(
        facts=["主导现象: 白酒板块领涨"],
        sources=[
            ChatSource(
                source_id="trace:snap-1",
                kind="trace",
                title="市场溯源 2026-07-24",
                snippet="交易日: 2026-07-24",
                captured_at=captured_at,
            )
        ],
        as_of=captured_at,
        skill_name="trace_lookup",
        degraded=False,
        raw={"date": "2026-07-24", "snapshot_id": "snap-1", "origin": "node"},
    )
    mock_llm = _mock_synth_llm(
        {
            "conclusion": "今日上涨由白酒带动",
            "basis_indices": [1],
            "confidence": "medium",
            "uncertainty": [],
            "answer_mode": "trace",
        }
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([ev]))

    assert result["insight"].basis == [ev]
    b = result["insight"].basis[0]
    assert b is ev  # 引用原对象
    assert b.facts == ev.facts
    assert b.sources == ev.sources
    assert b.sources[0].kind == "trace"
    assert b.sources[0].source_id == "trace:snap-1"
    assert b.as_of == ev.as_of
    assert b.raw == ev.raw
    assert b.raw["origin"] == "node"
    assert b.skill_name == "trace_lookup"
    assert b.degraded is False


@pytest.mark.asyncio
async def test_synth_answer_parse_error_still_degrades_safely() -> None:
    """真实解析异常（Pydantic ValidationError）→ 安全降级，不中断图执行。"""

    def _raise_parse_error(*args, **kwargs):
        # 模拟 json_mode 对非契约输出的真实校验失败
        SynthOutput.model_validate({})  # 缺 insight

    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=_raise_parse_error))
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state())

    assert result["final_response"].startswith("## 核心结论")
    assert result["insight"].confidence == "low"
    assert result["insight"].answer_mode == "validate"
    assert result["trace"].actual_mode == "validate"


def test_build_prompt_requires_structured_sections() -> None:
    """prompt 要求 Markdown 分节 + 结尾引导追问。"""
    from aistock_agent.graph.nodes.synth_answer import _build_prompt

    goal = InsightGoal(question="大盘今天怎么了?", intent="market_snapshot")
    prompt = _build_prompt(goal, [], "validate")

    assert "## 核心结论" in prompt
    assert "## 行情要点" in prompt
    assert "## 数据说明" in prompt
    assert "引导" in prompt or "继续问我" in prompt


def test_build_degraded_insight_structured_conclusion() -> None:
    """降级回答按分节结构输出，不输出一句"无法提供"。"""
    from aistock_agent.graph.nodes.synth_answer import _build_degraded_insight

    goal = InsightGoal(question="大盘今天怎么了?", intent="market_snapshot")
    evidence = Evidence(
        facts=["上证指数: 3804.69 (-0.62%)"],
        sources=[],
        as_of=datetime.now(UTC),
        degraded=True,
        degraded_reason="quick-snapshot 不可用",
        skill_name="market_snapshot",
    )
    insight = _build_degraded_insight(goal, [evidence], "validate", "test failure")

    assert insight.conclusion.startswith("## 核心结论")
    assert "## 行情要点" in insight.conclusion
    assert "上证指数" in insight.conclusion
