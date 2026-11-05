"""T8 变奏观察点 + 溯源文案（因果输出 spec §5 / §6）。

守门要点：
- 阻塞项判定必须用 `gate_blocked`（≠ `gate_hit`）：主线破位 / K 线不足时
  `gate_hit=False`，但档位同样被压到 0，用错会出现「0 档却说没有阻塞项」。
- 账本为唯一真相源：本文件所有输入都经 `derive_position_index` 产出，
  不手搓 PositionLedger，保证与真实生产路径同源。
"""
from __future__ import annotations

from aistock_agent.schemas.rhythm_master import CandidateOutlook, PositionLedger
from aistock_agent.services.mainline_engine import (
    EXCESS_WEAK,
    GAP_EXCESS,
    MIN_CANDIDATES,
    rank_candidates,
)
from aistock_agent.services.rhythm_engine import derive_position_index
from aistock_agent.services.rhythm_position_ledger import (
    build_watch_points,
    render_causality,
)

# 均线多头（base=3，无闸门）
BULL = [3000.0 + i * 5 for i in range(25)]
# 震荡（base=2，非多头且不满足闸门）
OSC = [float(3000 + (i % 7)) for i in range(30)]
# 均线空头（close<ma20 且 ma5<ma10<ma20 → 硬闸门）
BEAR = [3400.0 - i * 5 for i in range(25)]

STRONG = {"state": "established", "name": "AI 算力", "strength": "strong",
          "excess": 4.4, "breakdown": False}
WEAK = {"state": "established", "name": "AI 算力", "strength": "weak",
        "excess": 1.0, "breakdown": False}
NONE = {"state": "none", "name": None, "strength": None, "excess": 0.8}
UNAVAILABLE = {"state": "unavailable", "name": None, "strength": None}


def _ledger(closes, mainline=None, event_d=None, event_result=None) -> PositionLedger:
    _, ledger = derive_position_index(
        index_closes=closes, mainline=mainline,
        event_d=event_d, event_result=event_result,
    )
    return ledger


def _points(ledger, *, mainline_facts=None, event_d=None, closes=None, candidates=None):
    return build_watch_points(
        ledger,
        mainline_facts=mainline_facts,
        event_d=event_d,
        closes=closes or [],
        candidates=candidates,
    )


def _by_key(points) -> dict:
    return {p.key: p for p in points}


# ============ 1. 双闸门：两条观察点都不得声称「单独满足即可升档」 ============


def test_two_gates_make_all_watch_points_not_sufficient_alone() -> None:
    ledger = _ledger(BEAR, NONE)
    assert ledger.gate_hit is True
    assert ledger.mainline_state == "none"
    points = _points(ledger, mainline_facts=NONE, event_d=None, closes=BEAR)
    assert len(points) == 2
    assert {p.key for p in points} == {"gate", "mainline"}
    assert all(p.sufficient_alone is False for p in points)
    causality = render_causality(ledger)
    assert "同时" in causality.lead
    assert len(causality.lines) == 2


# ============ 2. 仅指数闸门：gate 项单独满足即可升档 ============


def test_single_gate_watch_point_is_sufficient_alone() -> None:
    ledger = _ledger(BEAR, STRONG)
    assert ledger.gate_hit is True
    assert ledger.mainline_state == "established"
    points = _points(ledger, mainline_facts=STRONG, event_d=None, closes=BEAR)
    assert [p.key for p in points] == ["gate"]
    assert points[0].sufficient_alone is True
    assert "ma5 回到 ma10 上方" in points[0].condition
    assert "差" in points[0].current_text


# ============ 3. 主线破位：gate_hit=False 仍须产出 gate 观察点 ============


def test_mainline_breakdown_still_produces_gate_watch_point() -> None:
    ml = {"state": "established", "name": "AI 算力", "strength": "strong",
          "excess": 1.8, "breakdown": True}
    ledger = _ledger(BULL, ml)
    assert ledger.gate_hit is False  # 指数本身没破位
    assert ledger.mainline_breakdown is True
    assert ledger.gate_blocked is True
    points = _points(ledger, mainline_facts=ml, event_d=None, closes=BULL)
    gate = _by_key(points)["gate"]
    assert gate.condition == "主线板块止跌回升，重回 20 日线上方"
    assert "AI 算力" in gate.current_text
    assert gate.sufficient_alone is True


# ============ 4. K 线不足：数据不足不猜条件 ============


def test_insufficient_bars_does_not_produce_gate_watch_point() -> None:
    short = [3000.0] * 10
    ledger = _ledger(short, STRONG)
    assert ledger.insufficient is True
    assert ledger.gate_blocked is True
    points = _points(ledger, mainline_facts=STRONG, event_d=None, closes=short)
    assert "gate" not in {p.key for p in points}


# ============ 5. 主线候选不可用：不伪造 condition、不带候选 ============


def test_mainline_unavailable_watch_point_has_no_condition_or_candidates() -> None:
    ledger = _ledger(OSC, UNAVAILABLE)
    assert ledger.mainline_state == "unavailable"
    points = _points(
        ledger, mainline_facts=UNAVAILABLE, event_d=None, closes=OSC,
        candidates=[CandidateOutlook(name="半导体", excess=1.2)],
    )
    ml = _by_key(points)["mainline"]
    assert ml.condition == "候选数据不足"
    assert ml.candidates is None


# ============ 6. 候选排名：降序 + 不足 limit 不补位 ============


def _cand(name: str, daily: float, bars: int = 25) -> dict:
    return {"name": name, "pct_chgs": [daily] * bars}


INDEX = [0.1] * 25


def test_rank_candidates_descending_and_no_padding() -> None:
    ranked = rank_candidates(
        [_cand("弱", 0.2), _cand("强", 1.0), _cand("中", 0.5)], INDEX
    )
    assert [c.name for c in ranked] == ["强", "中", "弱"]
    assert all(isinstance(c, CandidateOutlook) for c in ranked)
    assert ranked[0].excess > ranked[1].excess > ranked[2].excess


def test_rank_candidates_returns_two_when_only_two_scorable() -> None:
    ranked = rank_candidates([_cand("A", 1.0), _cand("B", 0.5)], INDEX)
    assert len(ranked) == 2
    assert [c.name for c in ranked] == ["A", "B"]


def test_rank_candidates_empty_and_unscorable_return_empty() -> None:
    assert rank_candidates([], INDEX) == []
    # 序列短于 window+1 → 算不出超额（不得用 0 伪装）
    assert rank_candidates([_cand("A", 1.0, bars=5)], INDEX) == []
    assert rank_candidates([_cand("A", 1.0)], []) == []


def _expected_excess_clause() -> str:
    """超额子句：EXCESS_WEAK<=0 时省掉超额门槛（否则写出「≥0.0pct」空条件）。"""
    if EXCESS_WEAK > 0:
        return f"5 日超额 ≥{EXCESS_WEAK:.1f}pct 且领先第 2 名 ≥{GAP_EXCESS:.1f}pct"
    return f"5 日超额领先第 2 名 ≥{GAP_EXCESS:.1f}pct"


def _expected_mainline_condition() -> str:
    return f"某板块 {_expected_excess_clause()}"


def test_mainline_watch_point_carries_top3_candidates() -> None:
    ledger = _ledger(OSC, NONE)
    cands = [CandidateOutlook(name=n, excess=e)
             for n, e in [("半导体", 0.8), ("算力", 0.3), ("CPO", 0.1), ("PCB", -0.2)]]
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC,
                         candidates=cands))["mainline"]
    assert [c.name for c in ml.candidates] == ["半导体", "算力", "CPO"]
    assert ml.condition == _expected_mainline_condition()
    # 超额已过门槛，真正卡住的是间距（0.8 − 0.3 = 0.5 < 2.0）
    assert ml.current_text == f"领先第 2 名 +0.50pct（需 ≥{GAP_EXCESS:.1f}）"


def test_mainline_condition_has_no_empty_threshold() -> None:
    """EXCESS_WEAK=0.0 时「≥0.0pct」是空门槛（任何正超额都过），文案不得出现。"""
    ledger = _ledger(OSC, NONE)
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC))["mainline"]
    assert "≥0.0pct" not in ml.condition
    assert f"领先第 2 名 ≥{GAP_EXCESS:.1f}pct" in ml.condition


def test_mainline_current_text_gap_when_excess_far_above_threshold() -> None:
    """实测回归（W6 接线样例）：最高 +24.2pct 远超 0.0 → 卡的是间距，不得写「差 -24.20」。

    本例候选仅 2 个（< MIN_CANDIDATES）且间距已达标 → 真实成因是有效候选数不足。
    """
    ledger = _ledger(OSC, NONE)
    cands = [CandidateOutlook(name="半导体", excess=24.2),
             CandidateOutlook(name="算力", excess=11.53)]
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC,
                         candidates=cands))["mainline"]
    assert "差 -" not in ml.current_text
    assert ml.current_text == f"仅 2 个候选，需 ≥{MIN_CANDIDATES} 个"
    assert ml.candidates and [c.excess for c in ml.candidates] == [24.2, 11.53]


def test_mainline_current_text_gap_when_enough_candidates() -> None:
    """候选数达标（≥3）但间距不足 → 指向间距，且差值取正。"""
    ledger = _ledger(OSC, NONE)
    cands = [CandidateOutlook(name="半导体", excess=24.2),
             CandidateOutlook(name="算力", excess=23.0),
             CandidateOutlook(name="CPO", excess=20.0)]
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC,
                         candidates=cands))["mainline"]
    assert "差 -" not in ml.current_text
    assert ml.current_text == f"领先第 2 名 +1.20pct（需 ≥{GAP_EXCESS:.1f}）"


def test_mainline_current_text_gap_insufficient() -> None:
    """间距不足 → 指向间距，且差值取正（达标值 − 当前值）。"""
    ledger = _ledger(OSC, NONE)
    cands = [CandidateOutlook(name="半导体", excess=1.0),
             CandidateOutlook(name="算力", excess=0.5)]
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC,
                         candidates=cands))["mainline"]
    assert ml.current_text == f"领先第 2 名 +0.50pct（需 ≥{GAP_EXCESS:.1f}）"


def test_mainline_current_text_excess_below_threshold() -> None:
    """超额未达门槛 → 指向超额缺口，且「差」必为正（不得出现负数差）。"""
    ledger = _ledger(OSC, NONE)
    cands = [CandidateOutlook(name="半导体", excess=-1.5),
             CandidateOutlook(name="算力", excess=-2.0)]
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC,
                         candidates=cands))["mainline"]
    assert "差 -" not in ml.current_text
    assert ml.current_text == f"最高 -1.5pct（差 {EXCESS_WEAK + 1.5:.2f}）"


def test_mainline_current_text_single_candidate() -> None:
    """单候选算不出间距（判据另收窄为 strong）→ 指向候选数，不硬算间距。"""
    ledger = _ledger(OSC, NONE)
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC,
                         candidates=[CandidateOutlook(name="半导体", excess=6.0)]))["mainline"]
    assert ml.current_text == f"仅 1 个候选，需 ≥{MIN_CANDIDATES} 个"


def test_mainline_watch_point_without_candidates_says_data_missing() -> None:
    ledger = _ledger(OSC, NONE)
    ml = _by_key(_points(ledger, mainline_facts=NONE, event_d=None, closes=OSC))["mainline"]
    assert ml.current_text == "候选数据不足"
    assert ml.candidates is None


# ============ 7. 全部已满足 → 无观察点 ============


def test_no_watch_points_when_everything_satisfied() -> None:
    ledger = _ledger(BULL, STRONG, event_d=1)
    assert ledger.final_index == 4
    assert ledger.gate_blocked is False
    points = _points(ledger, mainline_facts=STRONG, event_d=1, closes=BULL)
    assert points == []


# ============ 8. 排序：sufficient_alone 优先，其次 gate > mainline > event ============


def test_sort_puts_sufficient_alone_first() -> None:
    # 震荡 + 主线候选不可用 + 事件在窗口外：blocking 为空，base=2 → 事件项可单独 +1；
    # 主线项是「候选数据不足」，不能承诺单独满足即升档 → False（故被排到后面）。
    ledger = _ledger(OSC, UNAVAILABLE, event_d=5)
    assert ledger.base_index == 2 and ledger.adjust == 0
    assert ledger.gate_blocked is False
    points = _points(ledger, mainline_facts=UNAVAILABLE, event_d=5, closes=OSC)
    assert [p.key for p in points] == ["event", "mainline"]
    assert points[0].sufficient_alone is True
    assert points[1].sufficient_alone is False


def test_sort_ties_follow_gate_mainline_event_order() -> None:
    ledger = _ledger(BEAR, NONE, event_d=5)
    points = _points(ledger, mainline_facts=NONE, event_d=5, closes=BEAR)
    assert [p.key for p in points] == ["gate", "mainline", "event"]
    assert all(p.sufficient_alone is False for p in points)


# ============ 9. 溯源文案：2 / 1 / 0 条 ============


def test_causality_two_blocking_items() -> None:
    text = render_causality(_ledger(BEAR, NONE))
    assert text.lead == "两条闸门同时压到底，任一解除都不能升档"
    assert len(text.lines) == 2
    assert "跌破 20 日线" in text.lines[0] and "均线空头排列" in text.lines[0]
    assert text.lines[1] == f"无板块 {_expected_excess_clause()}，主线未成立"


def test_causality_single_blocking_item() -> None:
    text = render_causality(_ledger(BEAR, STRONG))
    assert text.lead == "唯一压制项："
    assert len(text.lines) == 1
    assert "跌破 20 日线" in text.lines[0]


def test_causality_no_blocking_item_gives_positive_lead() -> None:
    text = render_causality(_ledger(OSC, STRONG))
    assert text.lines == []
    assert text.lead == "均线震荡，base=2；主线成立·强，+1 → 3 档"


def test_causality_mainline_breakdown_line() -> None:
    ml = {"state": "established", "name": "AI 算力", "strength": "strong", "breakdown": True}
    text = render_causality(_ledger(BULL, ml))
    assert text.lines == ["主线板块破位"]
    assert text.lead == "唯一压制项："


# ============ 10. event_d=None 不产出事件观察点 ============


def test_event_watch_point_absent_when_event_d_is_none() -> None:
    ledger = _ledger(OSC, WEAK)
    points = _points(ledger, mainline_facts=WEAK, event_d=None, closes=OSC)
    assert "event" not in {p.key for p in points}


def test_event_watch_point_produced_outside_window() -> None:
    ledger = _ledger(OSC, WEAK, event_d=5)
    ev = _by_key(_points(ledger, mainline_facts=WEAK, event_d=5, closes=OSC))["event"]
    assert ev.current_text == "当前 d=5"
    assert ev.condition == "高影响事件进入 3 个交易日内且主线转强"


def test_event_watch_point_produced_in_window_with_weak_mainline() -> None:
    ledger = _ledger(OSC, WEAK, event_d=1)
    ev = _by_key(_points(ledger, mainline_facts=WEAK, event_d=1, closes=OSC))["event"]
    assert ev.current_text == "当前 d=1"
