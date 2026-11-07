"""T7 证据区温度因子文案（职责 spec §3）。

温度是 detect_stage 的 sentiment 因子来源，已从主视觉移除但保留在证据链中。
这里锁定三条口径：只给数值与差值、不引分档词、空归档不伪造。
"""
from __future__ import annotations

from aistock_agent.agents.workers.rhythm_master import _temperature_note

# 分档词归情绪温度所有，节奏侧不得借用（否则又回到「一名多义」）
_FORBIDDEN_WORDS = ("冰点", "低迷", "常温", "活跃", "亢奋")


def test_empty_series_returns_none() -> None:
    assert _temperature_note([]) is None


def test_single_point_has_no_delta() -> None:
    note = _temperature_note([{"date": "2026-11-03", "score": 42.5}])
    assert note == "情绪温度 42.5"
    assert "日" not in note  # 单点无法谈斜率，不得编造


def test_multi_point_reports_slope() -> None:
    series = [
        {"date": "2026-10-28", "score": 54.2},
        {"date": "2026-10-29", "score": 58.0},
        {"date": "2026-11-03", "score": 62.4},
    ]
    note = _temperature_note(series)
    assert note == "情绪温度 62.4（近 3 日 +8.2）"


def test_negative_slope_keeps_sign() -> None:
    series = [
        {"date": "2026-10-30", "score": 71.0},
        {"date": "2026-11-03", "score": 60.0},
    ]
    assert _temperature_note(series) == "情绪温度 60.0（近 2 日 -11.0）"


def test_never_uses_level_words() -> None:
    """任何分支都不得出现情绪分档词 —— 那是情绪温度的主场语汇。"""
    cases = [
        [],
        [{"date": "d1", "score": 10.0}],
        [{"date": "d1", "score": 10.0}, {"date": "d2", "score": 12.0}],
        [{"date": "d1", "score": 95.0}, {"date": "d2", "score": 99.0}],
    ]
    for series in cases:
        note = _temperature_note(series) or ""
        for word in _FORBIDDEN_WORDS:
            assert word not in note, f"{word} 不得出现在温度因子文案里：{note}"


def test_skips_non_numeric_scores() -> None:
    series = [
        {"date": "d1", "score": None},
        {"date": "d2"},
        {"date": "d3", "score": 50.0},
        {"date": "d4", "score": 55.5},
    ]
    assert _temperature_note(series) == "情绪温度 55.5（近 2 日 +5.5）"
