"""证据不足时不得触发分析师：避免空转烧 token。

规则：窗口内没有真实行情证据（收盘/上一收盘/盘中）时，不调用 LLM。
实测故障是早报取数失败、只剩 2 条日历事件，分析师仍被调用，花掉约 1.5 万
token 却写不出任何 claim。
"""

from __future__ import annotations

from types import SimpleNamespace

from aistock_agent.brain_v4.runtime.runner import _llm_evidence_ok


def _item(source: str) -> SimpleNamespace:
    return SimpleNamespace(source=source)


def test_calendar_only_evidence_is_rejected() -> None:
    calendar = [_item("node_api:calendar") for _ in range(10)]
    assert _llm_evidence_ok(calendar) is False


def test_empty_window_is_rejected() -> None:
    assert _llm_evidence_ok([]) is False


def test_close_snapshot_evidence_is_accepted() -> None:
    assert _llm_evidence_ok([_item("node_api:close-snapshot")]) is True


def test_previous_close_and_intraday_are_accepted() -> None:
    assert _llm_evidence_ok([_item("node_api:last-close-snapshot")]) is True
    assert _llm_evidence_ok([_item("node_api:intraday-sectors")]) is True
