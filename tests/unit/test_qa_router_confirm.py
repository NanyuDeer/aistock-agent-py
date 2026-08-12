"""QA Router 交互式确认单测（Phase 4-2，改进 13）。

覆盖（对齐 task-4 brief §测试）：
- ① 触发：闸门 2 resolve-miss + ≥2 可 resolve 多候选 → confirm 输出（替代澄清，无 clarification）
- ② <2 可 resolve → 既有澄清字节不变；len(multi)<2 → 不触发
- ③ confirm_timeout（阶段 2 重跑）→ 跳过触发直接走既有澄清
- ④ confirm_choice（阶段 2 续跑）→ 直接构造 SkillCall（不 resolve）；强预测词附加 predict 子目标
- ⑤ 闸门红线：合规/寒暄短路优先级不变（confirm_choice 存在时仍优先，不 bypass 闸门）
"""
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.qa_router import qa_router_node
from aistock_agent.prompts.general.system import CAPABILITY_REPLY, COMPLIANCE_REPLY
from aistock_agent.state.chat_schema import QuestionState

CLARIFICATION = "请提供 6 位股票代码后重试。"


@pytest.fixture(autouse=True)
def _no_real_node_resolve(monkeypatch):
    """默认 mock Node 名称解析，由用例显式 side_effect。"""
    monkeypatch.setattr(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(return_value=None),
    )


def _state(message: str, **extra) -> QuestionState:
    base: QuestionState = {
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
    base.update(extra)
    return base


def _resolve_map(mapping: dict[str, str]):
    return AsyncMock(side_effect=lambda name: mapping.get(name))


# ── ① 触发：resolve-miss + ≥2 可 resolve → confirm 替代澄清 ──────────────


@pytest.mark.asyncio
async def test_confirm_trigger_two_resolvable_candidates() -> None:
    """「我想了解一下贵州茅台和五粮液」主候选 resolve 失败 + 两名称均可解析 → confirm 输出。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        _resolve_map({"贵州茅台": "600519", "五粮液": "000858"}),
    ):
        result = await qa_router_node(_state("我想了解一下贵州茅台和五粮液"))

    assert "clarification" not in result
    assert "pending_clarification" not in result
    confirm = result["confirm"]
    assert confirm["question"] == "我想了解一下贵州茅台和五粮液"
    assert confirm["options"] == [
        {"key": "600519", "label": "贵州茅台(600519)"},
        {"key": "000858", "label": "五粮液(000858)"},
        {"key": "none", "label": "都不是"},
    ]
    assert result["plan"] == "direct"
    assert result["skill_calls"] == []
    assert result["complexity"] == "light"
    assert result["goal"].constraints.get("guardrail") == "resolve_confirm"


# ── ② 不触发：<2 可 resolve / len(multi)<2 → 既有澄清字节不变 ──────────────


@pytest.mark.asyncio
async def test_confirm_not_triggered_when_less_than_two_resolvable() -> None:
    """两候选仅 1 个可解析 → 不触发 confirm，既有澄清字节不变。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        _resolve_map({"五粮液": "000858"}),
    ):
        result = await qa_router_node(_state("我想了解一下贵州茅台和五粮液"))

    assert "confirm" not in result
    assert result["clarification"] == CLARIFICATION
    assert result["goal"].constraints.get("guardrail") == "resolve_miss"
    assert result["pending_clarification"] == {
        "question": "我想了解一下贵州茅台和五粮液",
        "intent": "stock_snapshot",
        "constraints": {"guardrail": "resolve_miss"},
    }


@pytest.mark.asyncio
async def test_confirm_not_triggered_when_single_candidate() -> None:
    """多候选 <2（「我想了解一下贵州茅台」）→ 不触发 confirm，走既有澄清。"""
    # autouse fixture resolve 恒 None：主候选失败 + 无多候选可解析
    result = await qa_router_node(_state("我想了解一下贵州茅台"))
    assert "confirm" not in result
    assert result["clarification"] == CLARIFICATION


# ── ③ confirm_timeout：阶段 2 重跑 → 跳过触发走既有澄清 ──────────────────


@pytest.mark.asyncio
async def test_confirm_timeout_skips_trigger_goes_clarification() -> None:
    """confirm_timeout=True 时即使两候选可解析也不触发 confirm（防无限循环）。"""
    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        _resolve_map({"贵州茅台": "600519", "五粮液": "000858"}),
    ):
        result = await qa_router_node(
            _state("我想了解一下贵州茅台和五粮液", confirm_timeout=True)
        )
    assert "confirm" not in result
    assert result["clarification"] == CLARIFICATION


@pytest.mark.asyncio
async def test_confirm_timeout_with_duplicated_messages_still_clarifies() -> None:
    """阶段 2 同 session 重跑：checkpointer 的 add_messages 把同一 HumanMessage
    追加进历史（messages=[m1,m1]）→ len(messages)<=1 守卫为 False。confirm_timeout
    回退必须不依赖消息数，无条件返回既有澄清，不落 LLM（防 D36 幻觉假代码）。
    """
    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think",
            return_value=AsyncMock(),
        ),
        patch(
            "aistock_agent.graph.nodes.qa_router.with_chat_structured_output",
            return_value=AsyncMock(),
        ) as structured_mock,
        patch(
            "aistock_agent.graph.nodes.qa_router.resolve_symbol",
            _resolve_map({"贵州茅台": "600519", "五粮液": "000858"}),
        ),
    ):
        m1 = HumanMessage(content="我想了解一下贵州茅台和五粮液")
        state = _state("我想了解一下贵州茅台和五粮液", confirm_timeout=True)
        state["messages"] = [m1, m1]  # 模拟 checkpointer 累积重复（run2）
        result = await qa_router_node(state)

    assert result["clarification"] == CLARIFICATION
    assert "confirm" not in result
    assert result["goal"].constraints.get("guardrail") == "resolve_miss"
    # 不能靠 side_effect=AssertionError（被业务层 except 吞掉 → 兜底也可能出澄清）；
    # 必须断言结构化 LLM 入口根本没被调用（落 LLM 即失败）。
    structured_mock.assert_not_called()


# ── ④ confirm_choice：阶段 2 续跑 → 直接构造 SkillCall（不 resolve）─────────


@pytest.mark.asyncio
async def test_confirm_choice_builds_skill_call_without_resolve() -> None:
    """confirm_choice 存在 → 直接构造 stock_snapshot，跳过名称提取/resolve。"""

    async def boom(name: str) -> None:
        raise AssertionError(f"confirm_choice 路径不应调用 resolve: {name}")

    with patch(
        "aistock_agent.graph.nodes.qa_router.resolve_symbol",
        AsyncMock(side_effect=boom),
    ):
        result = await qa_router_node(
            _state(
                "我想了解一下贵州茅台和五粮液",
                confirm_choice={"symbol": "600519", "label": "贵州茅台(600519)"},
            )
        )

    assert result["plan"] == "direct"
    assert len(result["skill_calls"]) == 1
    call = result["skill_calls"][0]
    assert call.skill_name == "stock_snapshot"
    assert call.args == {"symbol": "600519"}
    assert result["goal"].symbols == ["600519"]
    # 单轮 transient 输入不写回图状态输出
    assert "confirm" not in result
    assert "confirm_choice" not in result


@pytest.mark.asyncio
async def test_confirm_choice_with_strong_predict_appends_predict_subgoal() -> None:
    """confirm_choice + 强预测词（会涨）→ 附加 predict 子目标（对齐闸门 2 resolve 成功路径）。"""
    result = await qa_router_node(
        _state(
            "贵州茅台明天会涨吗",
            confirm_choice={"symbol": "600519", "label": "贵州茅台(600519)"},
        )
    )
    assert result["plan"] == "compose"
    calls = result["skill_calls"]
    assert [c.skill_name for c in calls] == ["stock_snapshot", "prediction"]
    assert calls[0].goal_id == "g1"
    assert calls[0].args == {"symbol": "600519"}
    assert calls[1].goal_id == "g2"
    assert calls[1].args == {"symbols": ["600519"]}
    assert [g.dimension for g in result["goals"]] == ["predict"]


# ── ⑤ 闸门红线：合规/寒暄短路优先级不变（不 bypass 闸门）──────────────────


@pytest.mark.asyncio
async def test_confirm_choice_does_not_bypass_compliance_gate() -> None:
    """闸门 0 合规红线：confirm_choice 存在时合规短路仍优先。"""
    result = await qa_router_node(
        _state(
            "贵州茅台能买吗",
            confirm_choice={"symbol": "600519", "label": "贵州茅台(600519)"},
        )
    )
    assert result["skill_calls"] == []
    assert result["final_response"] == COMPLIANCE_REPLY
    assert result["goal"].constraints.get("guardrail") == "compliance"


@pytest.mark.asyncio
async def test_confirm_choice_does_not_bypass_greeting_gate() -> None:
    """闸门 0.5 寒暄：confirm_choice 存在时寒暄短路仍优先。"""
    result = await qa_router_node(
        _state("你好", confirm_choice={"symbol": "600519", "label": "贵州茅台(600519)"})
    )
    assert result["final_response"] == CAPABILITY_REPLY
    assert result["goal"].constraints.get("guardrail") == "greeting"
