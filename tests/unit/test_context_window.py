"""trim_messages 纯函数单测（Phase 5 Task 1：消息滑动窗口 + 零 LLM 摘要）。

覆盖（对齐 brief §测试要求 ①-④ + 幂等）：
- ① ≤12 条 → 原样返回 + summary None
- ② 13+ 条 → window=最近 12 条 + summary 非空
- ③ summary ≤ summary_chars（默认 200）
- ④ 摘要含超窗用户问句 + 同轮 AI 回复片段
- 幂等：相同 messages → 相同 summary（无累积）
"""
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.utils.context_window import build_summary_context, trim_messages


def _turn_msgs(n_turns: int) -> list:
    """构造 n_turns 轮对话（每轮 1 条 Human + 1 条 AI），共 n_turns*2 条。"""
    out: list = []
    for i in range(n_turns):
        out.append(HumanMessage(content=f"用户问题{i}"))
        out.append(AIMessage(content=f"AI 回答{i}"))
    return out


# ── ① 短会话（≤12 条）原样返回 + summary None ──


def test_short_session_returns_unchanged() -> None:
    msgs = _turn_msgs(6)  # 12 条 = 窗口边界
    window, summary = trim_messages(msgs)
    assert summary is None
    assert window == msgs  # 原样，不改对象
    assert [m.content for m in window] == [m.content for m in msgs]


def test_empty_messages_no_summary() -> None:
    window, summary = trim_messages([])
    assert window == []
    assert summary is None


def test_single_turn_no_summary() -> None:
    window, summary = trim_messages(_turn_msgs(1))
    assert summary is None
    assert len(window) == 2


# ── ② 超窗（13+ 条）：window=最近 12 条 + summary 非空 ──


def test_over_window_returns_last_12() -> None:
    msgs = _turn_msgs(7)  # 14 条，超窗 2 条
    window, summary = trim_messages(msgs)
    assert summary is not None
    assert len(window) == 12
    assert [m.content for m in window] == [m.content for m in msgs[-12:]]
    # 超窗部分不在 window 内
    assert window[0].content == "用户问题1"


def test_13_messages_triggers_summary() -> None:
    msgs = _turn_msgs(6) + [HumanMessage(content="用户问题6")]
    window, summary = trim_messages(msgs)
    assert len(window) == 12
    assert summary is not None


# ── ③ summary ≤ 200 字（默认 summary_chars）──


def test_summary_within_default_chars() -> None:
    msgs = _turn_msgs(30)  # 60 条，超窗 48 条
    _, summary = trim_messages(msgs)
    assert summary is not None
    assert len(summary) <= 200


def test_summary_respects_custom_chars() -> None:
    msgs = _turn_msgs(30)
    _, summary = trim_messages(msgs, summary_chars=50)
    assert summary is not None
    assert len(summary) <= 50


# ── ④ 摘要内容：超窗用户问句 + 同轮 AI 回复片段 ──


def test_summary_contains_over_window_user_questions() -> None:
    msgs = _turn_msgs(8)  # 16 条，超窗 4 条（用户问题0/1 + AI 回答0/1）
    _, summary = trim_messages(msgs)
    assert summary is not None
    assert "用户：用户问题0" in summary
    assert "用户：用户问题1" in summary


def test_summary_contains_ai_snippet() -> None:
    msgs = _turn_msgs(7)  # 14 条，超窗 2 条（用户问题0 + AI 回答0）
    _, summary = trim_messages(msgs)
    assert summary is not None
    assert "AI：AI 回答0" in summary


# ── 幂等：相同 messages → 相同 summary（无累积）──


def test_summary_idempotent() -> None:
    msgs = _turn_msgs(10)
    _, s1 = trim_messages(msgs)
    _, s2 = trim_messages(msgs)
    assert s1 == s2


# ── 自定义 max_turns ──


def test_custom_max_turns_window() -> None:
    msgs = _turn_msgs(4)  # 8 条
    window, summary = trim_messages(msgs, max_turns=2)  # 窗口 4 条
    assert summary is not None
    assert len(window) == 4
    assert window[0].content == "用户问题2"


# ── build_summary_context 注入段 ──


def test_build_summary_context_none_empty() -> None:
    assert build_summary_context(None) == ""
    assert build_summary_context("") == ""


def test_build_summary_context_prefix() -> None:
    ctx = build_summary_context("用户：早期问题")
    assert "此前对话摘要" in ctx
    assert "用户：早期问题" in ctx
