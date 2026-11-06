"""T1 仓位档位序号化：derive_position_index + PositionLedger（因果输出 spec §4.1/§4.2）。

硬约束：判定判据必须与历史 derive_position_text 完全同源——本文件以
「同一输入下文案与序号一一对应」的同源验证兜住该约束。
"""

import pytest

from aistock_agent.schemas.rhythm_master import PositionBand, PositionLedger
from aistock_agent.services.rhythm_engine import (
    POSITION_LADDER,
    derive_position_index,
    derive_position_text,
)

# 均线多头（base=3，无闸门）
BULL = [3000.0 + i * 5 for i in range(25)]
# 震荡（base=2，非多头且不满足闸门）
OSC = [float(3000 + (i % 7)) for i in range(30)]
# 均线空头（close<ma20 且 ma5<ma10<ma20 → 硬闸门）
BEAR = [3400.0 - i * 5 for i in range(25)]

STRONG = {"state": "established", "name": "AI 算力", "strength": "strong", "excess": 1.8}
WEAK = {"state": "established", "name": "AI 算力", "strength": "weak", "excess": 1.0}
NONE = {"state": "none", "name": None, "strength": None, "excess": 0.8}


def _derive(closes, mainline=None, event_d=None, event_result=None, breadth=None):
    return derive_position_index(
        index_closes=closes, mainline=mainline,
        event_d=event_d, event_result=event_result, breadth=breadth,
    )


@pytest.mark.parametrize(
    ("closes", "mainline", "event_d", "event_result", "expected"),
    [
        # 0 档：硬闸门 / 无清晰主线
        (BEAR, None, None, None, 0),
        (OSC, NONE, None, None, 0),
        # 1 档：事件当日未落档 → 封顶轻仓
        (OSC, None, 0, None, 1),
        # 2 档：震荡基准 / 强主线但事件已落档不及预期（base 3 − 1）
        (OSC, None, None, None, 2),
        (BULL, STRONG, 0, "不及预期", 2),
        # 3 档：震荡 + 强主线 +1
        (OSC, STRONG, None, None, 3),
        # 4 档：多头 + 强主线（+1）/ 多头 + 强主线 + 事件临近（clamp 到 4）
        (BULL, STRONG, None, None, 4),
        (BULL, STRONG, 2, None, 4),
    ],
)
def test_five_ladder_levels(closes, mainline, event_d, event_result, expected) -> None:
    idx, ledger = _derive(closes, mainline, event_d, event_result)
    assert idx == expected
    assert ledger.final_index == expected
    assert 0 <= idx <= 4


def test_hard_gate_hits_zero_and_blocks_add() -> None:
    idx, ledger = _derive(BEAR, STRONG, 1, None)
    assert idx == 0
    assert ledger.gate_hit is True
    assert ledger.adjust == 0  # 闸门禁止 +1
    trend = ledger.items[0]
    assert trend.key == "trend"
    assert trend.status == "blocking"
    assert trend.judgement == "关键条件不满足"
    assert trend.effect == "一票否决：直接压到空仓"


def test_mainline_none_forces_zero() -> None:
    idx, ledger = _derive(OSC, NONE)
    assert idx == 0
    assert ledger.mainline_state == "none"
    ml = ledger.items[1]
    assert ml.key == "mainline"
    assert ml.status == "blocking"
    assert ml.judgement == "没有出现"
    assert ml.effect == "直接压到空仓"
    assert "最高超额 +0.8 个百分点" in ml.value_text


def test_insufficient_bars_does_not_raise_and_marks_missing() -> None:
    idx, ledger = _derive([3000.0] * 10, STRONG, 1, None)
    assert idx == 2  # base 保持 step0（2），闸门状态未知禁止 +1
    assert ledger.gate_hit is False
    assert ledger.adjust == 0
    trend = ledger.items[0]
    assert trend.status == "missing"
    assert trend.judgement == "数据不可用"
    assert "20" in trend.value_text


def test_mainline_absent_is_missing_not_neutral() -> None:
    _, ledger = _derive(OSC, None)
    ml = ledger.items[1]
    assert ml.status == "missing"
    assert ml.judgement == "数据不可用"
    assert ledger.mainline_breakdown is None  # 无主线输入 → 破位状态未知


def test_mainline_unavailable_is_missing_not_neutral() -> None:
    _, ledger = _derive(OSC, {"state": "unavailable", "name": None, "strength": None})
    ml = ledger.items[1]
    assert ml.status == "missing"
    assert ml.judgement == "数据不可用"
    assert ledger.mainline_state == "unavailable"


def test_unknown_mainline_state_normalized_to_unavailable() -> None:
    """闭集归一：未知 state 不得原样透出，统一按 unavailable 处理。"""
    idx, ledger = _derive(OSC, {"state": "weird_state", "name": "X", "strength": "strong"})
    assert idx == 2  # 判据不变：非 established / none → 不改档
    assert ledger.mainline_state == "unavailable"
    ml = ledger.items[1]
    assert ml.status == "missing"
    assert "weird_state" not in ml.value_text
    assert "状态异常" not in ml.value_text


def test_mainline_breakdown_blocks_and_is_recorded() -> None:
    """主线板块破位：final=0 且 gate_blocked/mainline_breakdown 均落库（T8 反解依据）。"""
    ml = {"state": "established", "name": "AI 算力", "strength": "strong", "breakdown": True}
    idx, ledger = _derive(BULL, ml, 1, None)
    assert idx == 0
    assert ledger.gate_hit is False  # 指数本身未破位
    assert ledger.mainline_breakdown is True
    assert ledger.gate_blocked is True
    assert ledger.base_index == 0
    item = ledger.items[1]
    assert item.status == "blocking"
    assert item.judgement == "主线破位"
    assert item.effect == "一票否决：直接压到空仓"


def test_mainline_not_breakdown_is_false_not_none() -> None:
    _, ledger = _derive(OSC, {"state": "established", "name": "AI 算力",
                              "strength": "weak", "breakdown": False})
    assert ledger.mainline_breakdown is False
    assert ledger.gate_blocked is False


def test_insufficient_bars_sets_insufficient_and_gate_blocked() -> None:
    _, ledger = _derive([3000.0] * 10, STRONG, 1, None)
    assert ledger.insufficient is True
    assert ledger.gate_blocked is True
    assert ledger.gate_hit is False
    assert ledger.mainline_breakdown is False  # 主线有输入且未破位


@pytest.mark.parametrize(
    ("breadth", "judgement", "status"),
    [
        ({"advance_ratio": 0.31}, "偏弱", "neutral"),
        ({"advance_ratio": 0.45}, "中性", "neutral"),
        ({"advance_ratio": 0.65}, "偏强", "neutral"),
        (None, "数据不可用", "missing"),
        ({}, "数据不可用", "missing"),
    ],
)
def test_breadth_item_reflects_input(breadth, judgement, status) -> None:
    _, ledger = _derive(OSC, None, breadth=breadth)
    item = ledger.items[3]
    assert item.key == "breadth"
    assert item.judgement == judgement
    assert item.status == status
    if breadth:
        assert "上涨占比" in item.value_text
        assert "%" in item.value_text
    # 宽度不改档：有无宽度输入，序号一致
    assert ledger.final_index == 2


def test_breadth_falls_back_to_counts() -> None:
    _, ledger = _derive(OSC, None, breadth={"advance_count": 3100, "total_count": 5000})
    item = ledger.items[3]
    assert item.value_text == "上涨占比 62%"
    assert item.judgement == "偏强"


def test_event_absent_is_missing() -> None:
    _, ledger = _derive(OSC, WEAK, None, None)
    ev = ledger.items[2]
    assert ev.status == "missing"
    assert ev.judgement == "数据缺失"


def test_event_day_without_result_caps_low() -> None:
    idx, ledger = _derive(BULL, STRONG, 0, None)
    assert idx == 1
    ev = ledger.items[2]
    assert ev.status == "blocking"
    assert ev.judgement == "仓位封顶轻仓"
    assert ev.effect == "仓位最多一成，不能再加"


def test_event_landed_bearish_overrides_mainline_add() -> None:
    idx, ledger = _derive(BULL, STRONG, 0, "不及预期")
    assert idx == 2
    assert ledger.base_index == 3
    assert ledger.adjust == -1
    ev = ledger.items[2]
    assert ev.status == "blocking"
    assert ev.judgement == "事件要求减仓"
    assert ev.effect == "减仓（不及预期）"
    # 主线 +1 被事件落档覆盖：不得记为 supporting
    ml = ledger.items[1]
    assert ml.status == "neutral"
    assert "覆盖" in ml.effect


def test_event_near_window_adds_one_only_with_strong_mainline() -> None:
    # 震荡 base=2 + 强主线 +1 + 事件临近 +1 = 4
    idx_strong, ledger_strong = _derive(OSC, STRONG, 1, None)
    assert idx_strong == 4
    assert ledger_strong.items[2].status == "supporting"
    assert ledger_strong.items[2].judgement == "事件支持加仓"

    idx_weak, ledger_weak = _derive(OSC, WEAK, 1, None)
    assert idx_weak == 2
    assert ledger_weak.items[2].status == "neutral"
    assert ledger_weak.items[2].judgement == "没有变化"


@pytest.mark.parametrize(
    ("closes", "mainline", "event_d", "event_result"),
    [
        (BEAR, None, None, None),
        (OSC, None, None, None),
        (OSC, NONE, None, None),
        (OSC, STRONG, 2, None),
        (BULL, STRONG, 0, "超预期"),
        ([3000.0] * 10, STRONG, 1, None),
    ],
)
def test_items_always_four_in_fixed_order(closes, mainline, event_d, event_result) -> None:
    _, ledger = _derive(closes, mainline, event_d, event_result)
    assert isinstance(ledger, PositionLedger)
    assert [i.key for i in ledger.items] == ["trend", "mainline", "event", "breadth"]
    assert len(ledger.items) == 4
    assert all(i.value_text and i.judgement and i.effect for i in ledger.items)
    assert {i.status for i in ledger.items} <= {"blocking", "neutral", "supporting", "missing"}
    # 本任务 watch_points 留空（由 T8 填充），字段必须存在
    assert ledger.watch_points == []
    assert ledger.ledger_version == "1.0"
    assert ledger.model_dump()["items"][0]["key"] == "trend"


@pytest.mark.parametrize(
    ("closes", "mainline", "event_d", "event_result"),
    [
        (BEAR, None, None, None),
        (BEAR, STRONG, 1, None),
        (OSC, None, 0, None),
        (OSC, None, None, None),
        (OSC, WEAK, None, None),
        (OSC, STRONG, None, None),
        (BULL, STRONG, None, None),
        (BULL, STRONG, 2, None),
        (BULL, STRONG, 0, None),
        (BULL, STRONG, 0, "超预期"),
        (BULL, STRONG, 0, "符合"),
        (BULL, STRONG, 0, "不及预期"),
        ([3000.0] * 10, STRONG, 1, None),
        ([3000.0] * 10, NONE, 0, None),
    ],
)
def test_text_and_index_are_same_source(closes, mainline, event_d, event_result) -> None:
    """同源验证：文案必等于 `POSITION_LADDER[idx]`，两者不得各自重算。"""
    idx, _ = _derive(closes, mainline, event_d, event_result)
    text = derive_position_text(
        index_closes=closes, mainline=mainline,
        event_d=event_d, event_result=event_result,
    )
    assert text == "建议仓位：" + POSITION_LADDER[idx]


def test_ledger_arithmetic_base_plus_adjust() -> None:
    """无 clamp / 无 d==0 未落档封顶时，final = base + adjust。"""
    for closes, mainline, event_d, event_result in [
        (BEAR, None, None, None),
        (OSC, STRONG, None, None),
        (BULL, STRONG, 0, "超预期"),
        (BULL, STRONG, 0, "不及预期"),
        (OSC, NONE, None, None),
    ]:
        idx, ledger = _derive(closes, mainline, event_d, event_result)
        assert ledger.base_index is not None
        assert ledger.base_index + ledger.adjust == idx


def test_position_band_index_field_defaults_none() -> None:
    band = PositionBand(text="轻仓~三成", action="hold", direction="neutral")
    assert band.index is None
    assert PositionBand(
        text="轻仓~三成", action="hold", direction="neutral", index=1
    ).index == 1
