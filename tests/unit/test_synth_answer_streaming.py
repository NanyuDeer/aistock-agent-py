"""synth_answer 节级伪流式（D9）单元测试（改进 17 回答内容流式显示，Task 2）。

D9 决策（2026-08-13 用户裁定；Task 0 spike 门禁未通过）：json_mode + Pydantic schema 的
astream 只产出唯一完整实例（无 per-chunk 增量）→ 生产链维持同步 ainvoke，终态文本按
markdown 分节 dispatch chat_content_delta（节级伪流式，前端打字机提供逐字动画）。

本文件把 Task 2 brief 的测试意图清单适配到 D9 语义（机制从「LLM astream 增量 diff」改为
「同步 ainvoke + 按 markdown 分节 dispatch」）：
- 节级 dispatch：join(deltas) == final_response 字节全等、任意累积前缀是字节前缀（硬约束 2）
- 多子目标：节标题先发（渐进反馈）、正文后发；DISCLAIMER/风险段按最终字节序列收尾
- content_reset（D4/M5 统一语义）：已流式且终态文本非已流式内容前缀 → 显式整段替换；
  触发面覆盖校验失败降级 / 节降级 / 流式中途异常（mock 使对应路径走降级）
- hint 跨界一致（D5）：trading_session_status 单次取值 + 缓存前缀，流式与 DONE 文本共用
- 计费收口（硬约束 3）：不换 astream、不新增 LLM 调用（get_deep_think/ainvoke 次数不变）
- 守卫：payload 恒 dict {"content": str}，空串/None 不分发，分发失败静默（「永不 500」）
"""
from __future__ import annotations

from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from pydantic import ValidationError

from aistock_agent.graph.nodes import synth_answer
from aistock_agent.graph.nodes.synth_answer import (
    SynthOutput,
    _dispatch_content_deltas,
    _finalize_content_stream,
    _SectionResult,
    _split_content_deltas,
    _synth_multi_goal,
    synth_answer_node,
)
from aistock_agent.prompts.general.system import (
    RISK_DISCLAIMER,
    RISK_DISCLAIMER_CONSERVATIVE,
    RISK_DISCLAIMER_STRONG,
)
from aistock_agent.schemas.chat_contract import (
    ChatSource,
    Evidence,
    InsightGoal,
    SubGoal,
)

# ─── 公共辅助 ──────────────────────────────────────────────────────────

_DELTA = "chat_content_delta"
_RESET = "chat_content_reset"


def _evidence(skill: str, facts: list[str]) -> Evidence:
    return Evidence(
        facts=facts,
        sources=[],
        as_of=datetime.now(UTC),
        skill_name=skill,
    )


def _state(message: str = "茅台现在多少钱") -> dict:
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


def _state_with_evidences(evidences: list[Evidence], message: str = "今天为什么涨") -> dict:
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


def _mock_synth_llm_raise(exc: Exception) -> MagicMock:
    """构造 ainvoke 抛异常的 mock LLM（模拟校验失败 / 网络异常）。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(side_effect=exc))
    )
    return mock_llm


def _subgoal(goal_id: str, dimension: str, question: str) -> SubGoal:
    return SubGoal(
        id=goal_id,
        question=question,
        intent="market_snapshot",  # type: ignore[arg-type]
        dimension=dimension,  # type: ignore[arg-type]
    )


def _quote_evidence_non_today() -> Evidence:
    """非交易日触发所需的行情证据（market_snapshot 最近交易日回退，used_last_close=True）。"""
    return Evidence(
        facts=["数据日期：07-31", "上证指数(07-31): 3832.26 (+0.72%)"],
        sources=[
            ChatSource(
                source_id="market:a_share:quick:20260731",
                kind="realtime_quote",
                title="A 股最近交易日快照 (20260731)",
                snippet="",
                captured_at=datetime.now(UTC),
            )
        ],
        as_of=datetime.now(UTC),
        degraded=False,
        skill_name="market_snapshot",
        raw={
            "scope": "both",
            "a_share_success": True,
            "used_last_close": True,
            "trade_date": "20260731",
        },
    )


def _record_stream_events(monkeypatch) -> list[tuple[str, dict]]:
    """patch 模块内 adispatch_custom_event，记录 (event_name, payload)。"""
    events: list[tuple[str, dict]] = []

    async def _record(name: str, data: dict) -> None:
        events.append((name, data))

    monkeypatch.setattr(synth_answer, "adispatch_custom_event", _record)
    return events


def _delta_contents(events: list[tuple[str, dict]]) -> list[str]:
    return [data["content"] for name, data in events if name == _DELTA]


def _delta_entries(events: list[tuple[str, dict]]) -> list[tuple[str, str]]:
    """(name, {"content": str}) 事件列表 → (name, content) 条目（供前缀链断言复用）。"""
    return [(name, data["content"]) for name, data in events if name == _DELTA]


def _reset_contents(events: list[tuple[str, dict]]) -> list[str]:
    return [data["content"] for name, data in events if name == _RESET]


def _assert_byte_prefix_chain(entries: list[tuple[str, str]], final_text: str) -> None:
    """断言 delta 内容累积恒为 final_text 的字节前缀，且最终字节全等（硬约束 2）。

    entries 为 (name, content) 序列（"await" 标记跳过，供多子目标交错断言复用）。
    """
    accumulated = ""
    for name, content in entries:
        if name == "await":
            continue
        assert isinstance(content, str) and content
        accumulated += content
        assert final_text.startswith(accumulated), (
            f"字节前缀被破坏: 累积={accumulated!r} 终态={final_text!r}"
        )
    assert accumulated == final_text, f"字节全等失败: {accumulated!r} != {final_text!r}"


# ─── 纯函数：_split_content_deltas（D9 节级切分）──────────────────────────


def test_split_content_deltas_no_header_single_section() -> None:
    """无 `##` 节头 → 整段单节。"""
    assert _split_content_deltas("没有分节的一段文字。") == ["没有分节的一段文字。"]


def test_split_content_deltas_by_section_headers() -> None:
    """带 `##` 节头 → 按节顺序，join(deltas) == 原文字节全等。"""
    text = (
        "## 核心结论\n一句话回答。\n\n## 行情要点\n- 上证指数 3804.69\n\n"
        "## 数据说明\n数据日期 07-31。"
    )
    deltas = _split_content_deltas(text)
    assert deltas == [
        "## 核心结论\n一句话回答。\n\n",
        "## 行情要点\n- 上证指数 3804.69\n\n",
        "## 数据说明\n数据日期 07-31。",
    ]
    assert "".join(deltas) == text


def test_split_content_deltas_hint_prefix_own_leading_delta() -> None:
    """hint 文首前缀（若文本以其开头）→ 独立首增量；文末风险段 → 独立末增量。"""
    hint = "今天是 A 股非交易日（2026-08-02 周日），暂无当日行情数据。\n\n"
    text = f"{hint}## 核心结论\nx\n\n{RISK_DISCLAIMER}"
    deltas = _split_content_deltas(text, hint_prefix=hint)
    assert deltas == [hint, "## 核心结论\nx", f"\n\n{RISK_DISCLAIMER}"]
    assert "".join(deltas) == text


def test_split_content_deltas_hint_not_applied_no_split() -> None:
    """终态文本不以缓存 hint 开头（LLM 结论已含提示被去重）→ 不做 hint 切分（字节全等兜底）。"""
    text = f"## 核心结论\nx\n\n{RISK_DISCLAIMER}"
    assert _split_content_deltas(text, hint_prefix="今天是 A 股非交易日\n\n") == [
        "## 核心结论\nx",
        f"\n\n{RISK_DISCLAIMER}",
    ]


@pytest.mark.parametrize(
    "disclaimer",
    [RISK_DISCLAIMER, RISK_DISCLAIMER_STRONG, RISK_DISCLAIMER_CONSERVATIVE],
)
def test_split_content_deltas_trailing_disclaimer_variants(disclaimer: str) -> None:
    """文末三档风险段均识别为独立末增量（D28 代码拼接形态）。"""
    text = f"## 核心结论\nx\n\n{disclaimer}"
    assert _split_content_deltas(text) == ["## 核心结论\nx", f"\n\n{disclaimer}"]


# ─── 单意图路径：节级 dispatch + 字节全等 ─────────────────────────────────


@pytest.mark.asyncio
async def test_single_intent_dispatches_sections_byte_exact(monkeypatch) -> None:
    """单意图：3 节结论按节顺序 dispatch，join(deltas) == final_response 字节全等，风险段独立末增量。"""  # noqa: E501
    events = _record_stream_events(monkeypatch)
    mock_llm = _mock_synth_llm(
        {
            "conclusion": (
                "## 核心结论\n一句话回答。\n\n"
                "## 行情要点\n- 上证指数 3804.69\n\n"
                "## 数据说明\n数据日期 07-31。"
            ),
            "basis_indices": [],
            "confidence": "low",
            "uncertainty": [],
            "answer_mode": "trace",
        }
    )
    with (
        patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm),
        patch(
            "aistock_agent.graph.nodes.synth_answer.trading_session_status",
            return_value=("trading", ""),
        ),
    ):
        result = await synth_answer_node(_state_with_evidences([]))

    deltas = _delta_contents(events)
    assert deltas == [
        "## 核心结论\n一句话回答。\n\n",
        "## 行情要点\n- 上证指数 3804.69\n\n",
        "## 数据说明\n数据日期 07-31。",
        f"\n\n{RISK_DISCLAIMER}",
    ]
    assert deltas[-1] == f"\n\n{RISK_DISCLAIMER}"
    _assert_byte_prefix_chain(_delta_entries(events), result["final_response"])
    assert _reset_contents(events) == []


@pytest.mark.asyncio
async def test_single_intent_no_header_dispatches_whole(monkeypatch) -> None:
    """单意图：无 `##` 节头 → 正文单节整体 delta（风险段独立末增量）。"""
    events = _record_stream_events(monkeypatch)
    mock_llm = _mock_synth_llm(
        {
            "conclusion": "今日市场整体平稳。",
            "basis_indices": [],
            "confidence": "low",
            "uncertainty": [],
            "answer_mode": "trace",
        }
    )
    with (
        patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm),
        patch(
            "aistock_agent.graph.nodes.synth_answer.trading_session_status",
            return_value=("trading", ""),
        ),
    ):
        result = await synth_answer_node(_state_with_evidences([]))

    deltas = _delta_contents(events)
    assert deltas == ["今日市场整体平稳。", f"\n\n{RISK_DISCLAIMER}"]
    _assert_byte_prefix_chain(_delta_entries(events), result["final_response"])


# ─── 多子目标：节标题先发 + 正文后发 + 字节前缀 ────────────────────────────


@pytest.mark.asyncio
async def test_multi_goal_header_before_body_and_byte_prefix(monkeypatch) -> None:
    """多子目标：节标题在 ainvoke 前先发（渐进反馈）、正文后发；全程字节前缀 + 字节全等。"""
    order: list[tuple[str, str]] = []

    async def _record(name: str, data: dict) -> None:
        order.append(("dispatch", data["content"]))

    async def _fake_synth_section(
        goal: InsightGoal, evidences: list[Evidence], summary_context: str = ""
    ) -> _SectionResult:
        order.append(("await", goal.question))
        return _SectionResult(
            f"结论{goal.question}", [], "medium", [], "validate"
        )

    monkeypatch.setattr(synth_answer, "adispatch_custom_event", _record)
    monkeypatch.setattr(synth_answer, "_synth_section", _fake_synth_section)
    goals = [_subgoal("g1", "validate", "问题一"), _subgoal("g2", "validate", "问题二")]
    result = await _synth_multi_goal(
        {"plan": "compose", "skill_calls": []},
        InsightGoal(question="x", intent="market_snapshot"),
        [],
        goals,
    )  # type: ignore[arg-type]

    # 节标题先发、正文后发（每节的 ainvoke 夹在标题与正文之间）
    assert order == [
        ("dispatch", "## 问题一\n\n"),
        ("await", "问题一"),
        ("dispatch", "结论问题一"),
        ("dispatch", "\n\n## 问题二\n\n"),
        ("await", "问题二"),
        ("dispatch", "结论问题二"),
        ("dispatch", f"\n\n{RISK_DISCLAIMER}"),
    ]
    events = [(name, data) for name, data in order if name == "dispatch"]
    _assert_byte_prefix_chain(events, result["final_response"])
    assert _reset_contents(events) == []


@pytest.mark.asyncio
async def test_multi_goal_hint_dispatched_first(monkeypatch) -> None:
    """多子目标 + 非交易日：hint 前缀最先分发，最终文本中 hint 恰好一次且位置字节一致。"""
    events = _record_stream_events(monkeypatch)
    ev = _quote_evidence_non_today()
    goals = [_subgoal("g1", "validate", "大盘当前表现")]
    expected_hint = (
        "今天是 A 股非交易日（2026-08-02 周日），当日无行情数据。以下为最近交易日（"
        "2026-07-31 周五）收盘数据（非今日实时）。\n\n"
    )
    monkeypatch.setattr(
        synth_answer,
        "_synth_section",
        AsyncMock(
            return_value=_SectionResult("## 核心结论\n正常结论", [ev], "medium", [], "validate")
        ),
    )
    monkeypatch.setattr(
        synth_answer, "trading_session_status", lambda: ("non_trading_day", "今天非交易日")
    )
    monkeypatch.setattr(synth_answer, "shanghai_today", lambda: date(2026, 8, 2))
    monkeypatch.setattr(synth_answer, "prev_trading_day", lambda d=None: date(2026, 7, 31))

    result = await _synth_multi_goal(
        {"plan": "compose", "skill_calls": []},
        InsightGoal(question="x", intent="market_snapshot"),
        [ev],
        goals,
    )  # type: ignore[arg-type]

    deltas = _delta_contents(events)
    assert deltas[0] == expected_hint  # hint 最先分发
    final = result["final_response"]
    assert final.startswith(expected_hint)
    assert final.count("今天是 A 股非交易日") == 1  # hint 恰好一次
    _assert_byte_prefix_chain(_delta_entries(events), final)
    assert _reset_contents(events) == []


# ─── content_reset（D4/M5 统一语义）三触发面 ─────────────────────────────


async def _run_degraded_single_intent(
    monkeypatch, exc: Exception
) -> tuple[list[tuple[str, dict]], dict]:
    """单意图降级路径执行（hint 已流式 + 终态降级文本不含 hint → reset 触发）。"""
    events = _record_stream_events(monkeypatch)
    status_values = iter([("non_trading_day", "今天非交易日"), ("trading", "")])
    monkeypatch.setattr(synth_answer, "trading_session_status", lambda: next(status_values))
    monkeypatch.setattr(synth_answer, "shanghai_today", lambda: date(2026, 8, 2))
    monkeypatch.setattr(synth_answer, "prev_trading_day", lambda d=None: date(2026, 7, 31))
    ev = _quote_evidence_non_today()
    mock_llm = _mock_synth_llm_raise(exc)
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([ev]))
    return events, result


@pytest.mark.asyncio
async def test_content_reset_on_validation_failure_degraded(monkeypatch) -> None:
    """触发面 1（结构化校验失败 ValidationError）：已流式 hint 非降级终态前缀 → content_reset。"""
    events, result = await _run_degraded_single_intent(
        monkeypatch, ValidationError.from_exception_data("SynthOutput", [])
    )
    deltas = _delta_contents(events)
    resets = _reset_contents(events)
    assert len(deltas) == 1 and deltas[0].startswith("今天是 A 股非交易日")  # hint 已流式
    assert len(resets) == 1
    assert resets[0] == result["final_response"]  # reset 携带完整降级终态文本
    assert result["final_response"].startswith("## 核心结论")  # 降级文本
    assert result["insight"].confidence == "low"


@pytest.mark.asyncio
async def test_content_reset_on_llm_network_error_degraded(monkeypatch) -> None:
    """触发面 3（流式中途 LLM 网络异常走 except）：已流式 hint 非降级终态前缀 → content_reset。"""
    events, result = await _run_degraded_single_intent(monkeypatch, RuntimeError("network error"))
    deltas = _delta_contents(events)
    resets = _reset_contents(events)
    assert len(deltas) == 1 and deltas[0].startswith("今天是 A 股非交易日")
    assert len(resets) == 1
    assert resets[0] == result["final_response"]
    assert result["final_response"].startswith("## 核心结论")


@pytest.mark.asyncio
async def test_content_reset_on_section_degraded_keeps_prefix(monkeypatch) -> None:
    """触发面 2（多子目标节降级）：降级正文按节正常分发，前缀保持、无多余 reset。

    D9 下 `_synth_section` 吞异常返回 degraded 节时，正文整段到达（无半截流式内容），
    降级正文仍是终态文本的连续切片 → 前缀链不断 → 不触发 reset（与 D4 语义一致）。
    """
    order: list[tuple[str, str]] = []

    async def _record(name: str, data: dict) -> None:
        order.append(("dispatch", data["content"]))

    async def _degraded_section(
        goal: InsightGoal, evidences: list[Evidence], summary_context: str = ""
    ) -> _SectionResult:
        order.append(("await", goal.question))
        return _SectionResult(
            "## 行情要点\n- 降级事实",
            [],
            "low",
            ["综合失败: boom"],
            "validate",
            degraded=True,
        )

    monkeypatch.setattr(synth_answer, "adispatch_custom_event", _record)
    monkeypatch.setattr(synth_answer, "_synth_section", _degraded_section)
    monkeypatch.setattr(synth_answer, "trading_session_status", lambda: ("trading", ""))
    goals = [_subgoal("g1", "validate", "问题一")]
    result = await _synth_multi_goal(
        {"plan": "compose", "skill_calls": []},
        InsightGoal(question="x", intent="market_snapshot"),
        [],
        goals,
    )  # type: ignore[arg-type]

    assert order == [
        ("dispatch", "## 问题一\n\n"),
        ("await", "问题一"),
        ("dispatch", "## 行情要点\n- 降级事实"),
        ("dispatch", f"\n\n{RISK_DISCLAIMER}"),
    ]
    events = [(name, data) for name, data in order if name == "dispatch"]
    assert "降级事实" in result["final_response"]
    _assert_byte_prefix_chain(events, result["final_response"])
    assert _reset_contents(events) == []
    assert result["insight"].confidence == "low"


@pytest.mark.asyncio
async def test_finalize_content_stream_reset_semantics(monkeypatch) -> None:
    """统一收尾语义：已流式且终态非前缀 → reset；前缀成立 / 未开始流式 → 不 reset。"""
    events = _record_stream_events(monkeypatch)
    # 已流式 + 终态非已流式前缀 → 显式整段替换
    await _finalize_content_stream("## 核心结论\n半截内容", "完全不同的降级终态文本")
    assert _reset_contents(events) == ["完全不同的降级终态文本"]
    # 终态是已流式内容前缀扩展 → 不 reset（DONE 前缀补尾）
    await _finalize_content_stream("## 核心结论\n半截", "## 核心结论\n半截+补尾")
    assert len(_reset_contents(events)) == 1
    # 未开始流式（nothing dispatched）→ 不 reset（无内容可替换）
    await _finalize_content_stream("", "## 核心结论\n降级全文")
    assert len(_reset_contents(events)) == 1


# ─── hint 跨界一致（D5：trading_session_status 单次取值 + 缓存前缀）────────


@pytest.mark.asyncio
async def test_hint_single_read_and_once_in_final(monkeypatch) -> None:
    """单意图成功路径：trading_session_status 恰好 1 次取值；hint 前缀最先分发且终态仅 1 次。"""
    events = _record_stream_events(monkeypatch)
    status_calls: list[str] = []
    monkeypatch.setattr(
        synth_answer,
        "trading_session_status",
        lambda: status_calls.append("x") or ("non_trading_day", "今天非交易日"),
    )
    monkeypatch.setattr(synth_answer, "shanghai_today", lambda: date(2026, 8, 2))
    monkeypatch.setattr(synth_answer, "prev_trading_day", lambda d=None: date(2026, 7, 31))
    mock_llm = _mock_synth_llm(
        {
            "conclusion": "## 核心结论\n白酒板块今日表现活跃。",
            "basis_indices": [],
            "confidence": "low",
            "uncertainty": [],
            "answer_mode": "trace",
        }
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([_quote_evidence_non_today()]))

    assert len(status_calls) == 1  # D5：时段状态只取一次（流式前缀与 DONE 文本共用）
    expected_hint = (
        "今天是 A 股非交易日（2026-08-02 周日），当日无行情数据。以下为最近交易日（"
        "2026-07-31 周五）收盘数据（非今日实时）。\n\n"
    )
    deltas = _delta_contents(events)
    assert deltas[0] == expected_hint
    final = result["final_response"]
    assert final.startswith(expected_hint)
    assert final.count("今天是 A 股非交易日") == 1
    _assert_byte_prefix_chain(_delta_entries(events), final)
    assert _reset_contents(events) == []


# ─── 计费收口（硬约束 3：不换 astream、不新增 LLM 调用）───────────────────


@pytest.mark.asyncio
async def test_billing_llm_calls_unchanged_single_intent() -> None:
    """单意图：get_deep_think 1 次、structured_llm.ainvoke 恰好 1 次（与改造前一致）。"""
    mock_llm = _mock_synth_llm(
        {
            "conclusion": "## 核心结论\n正常结论",
            "basis_indices": [],
            "confidence": "low",
            "uncertainty": [],
            "answer_mode": "trace",
        }
    )
    with patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm):
        result = await synth_answer_node(_state_with_evidences([]))
    mock_llm.with_structured_output.assert_called_once()
    mock_llm.with_structured_output.return_value.ainvoke.assert_awaited_once()
    assert result["final_response"]


@pytest.mark.asyncio
async def test_billing_llm_calls_unchanged_multi_goal(monkeypatch) -> None:
    """多子目标：每节恰好 1 次 ainvoke（改造不新增调用，计费口径零变化）。"""
    deep_think_calls = 0
    ainvoke_mocks: list[AsyncMock] = []

    def _fake_deep_think() -> MagicMock:
        nonlocal deep_think_calls
        deep_think_calls += 1
        structured = MagicMock()
        structured.ainvoke = AsyncMock(
            return_value=SynthOutput.model_validate(
                {
                    "insight": {
                        "conclusion": "## 核心结论\n正常结论",
                        "basis_indices": [],
                        "confidence": "low",
                        "uncertainty": [],
                        "answer_mode": "validate",
                    }
                }
            )
        )
        ainvoke_mocks.append(structured.ainvoke)
        llm = MagicMock()
        llm.with_structured_output = MagicMock(return_value=structured)
        return llm

    monkeypatch.setattr(synth_answer, "get_deep_think", _fake_deep_think)
    monkeypatch.setattr(synth_answer, "trading_session_status", lambda: ("trading", ""))
    goals = [_subgoal("g1", "validate", "问题一"), _subgoal("g2", "validate", "问题二")]
    state = {
        "messages": [HumanMessage(content="x")],
        "goal": InsightGoal(question="x", intent="market_snapshot"),
        "plan": "compose",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "goals": goals,
    }
    result = await synth_answer_node(state)  # type: ignore[arg-type]

    assert deep_think_calls == 2  # 每节 1 次（与改造前一致）
    assert len(ainvoke_mocks) == 2 and all(m.await_count == 1 for m in ainvoke_mocks)
    assert result["final_response"]


# ─── 守卫：payload 恒 dict、空串不分发、分发失败静默（永不 500）────────────


@pytest.mark.asyncio
async def test_dispatch_guard_empty_skipped_and_payload_dict(monkeypatch) -> None:
    """空串/None 不分发；payload 恒 dict {"content": str}（防脏增量）。"""
    events = _record_stream_events(monkeypatch)
    await _dispatch_content_deltas(["", " 有效内容  ", ""])
    assert events == [(_DELTA, {"content": " 有效内容  "})]
    await _dispatch_content_deltas([])
    assert len(events) == 1


@pytest.mark.asyncio
async def test_dispatch_failure_silent_never_500(monkeypatch) -> None:
    """分发失败（无 run 上下文等）静默吞掉，不阻断回答（"永不 500"）。"""

    async def _boom(name: str, data: dict) -> None:
        raise RuntimeError("no parent run id")

    monkeypatch.setattr(synth_answer, "adispatch_custom_event", _boom)
    # 不抛异常即可（真实分发路径在无图上下文的单测里同样被吞）
    await _dispatch_content_deltas(["## 节一", "## 节二"])
    await _finalize_content_stream("已分发", "不同文本")
