"""主线/仓位节奏相关确定性规则单测（spec §4.4/§5.2/§5.4）。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_calendar import is_high_importance_event


@pytest.mark.parametrize("title", [
    "美联储 9 月议息会议",
    "美国 8 月 CPI 数据公布",
    "9 月 FOMC 利率决议",
])
def test_macro_titles_promoted(title):
    assert is_high_importance_event(title, "L3") is True


@pytest.mark.parametrize("title", [
    "某公司回应关税影响，公司股价大跌",
    "英伟达发布新一代 GPU",
    "300750 宁王 8 月销量",
    "美联储官员内部讲话纪要（正文超过四十个字符的标题会被截断处理以保护布局）",
])
def test_company_or_long_titles_not_promoted(title):
    assert is_high_importance_event(title, "L3") is False


def test_elevated_source_l2_loose_match():
    assert is_high_importance_event("美 CPI 前瞻", "L2") is True
    assert is_high_importance_event("CPI", None) is True


def test_load_event_window_normalizes_importance():
    from aistock_agent.services import event_calendar as ec

    with patch.object(ec, "node_api") as api:
        api.get_calendar_events = AsyncMock(return_value=[
            {"date": "2026-09-15", "title": "美联储 9 月议息会议", "importance": "medium",
             "source": "L3", "type": "macro"},
            {"date": "2026-09-16", "title": "某公司回应关税影响", "importance": "medium",
             "source": "L3", "type": "macro"},
        ])
        win = asyncio.run(ec.load_event_window("2026-09-15"))
    assert win.events[0]["importance"] == "high"
    assert win.events[1]["importance"] == "medium"
    assert len(win.high_events) == 1


from aistock_agent.services.rhythm_engine import (
    EVENT_BRANCH_MAX_D, POSITION_LADDER, build_event_branch,
)


def test_position_ladder_is_absolute():
    # H1：绝对成数锚定
    assert POSITION_LADDER[0] == "空仓观望"
    assert "满仓" in POSITION_LADDER[4]
    assert len(POSITION_LADDER) == 5


def test_event_branch_none_when_d_beyond_max():
    ev = {"date": "2026-09-22", "title": "FOMC 议息", "importance": "high"}
    # origin=2026-09-16(周三)：交易日差到 09-22(周二) = {09-17,09-18,09-21,09-22}=4 > 3
    assert build_event_branch(ev, origin_date="2026-09-16") == []


def test_event_branch_kept_when_d_within_max():
    ev = {"date": "2026-09-18", "title": "FOMC 议息", "importance": "high"}
    out = build_event_branch(ev, origin_date="2026-09-16")
    assert len(out) == 3
    assert "不改变主档位" in out[0]["conclusion"]["note"]


def test_event_branch_without_origin_unchanged():
    ev = {"date": "2026-09-22", "title": "FOMC 议息", "importance": "high"}
    assert len(build_event_branch(ev)) == 3  # 无 origin 不启用 d 约束（向后兼容）


def test_event_branch_non_high_empty():
    assert build_event_branch({"date": "2026-09-15", "title": "X", "importance": "medium"}) == []


import pytest

from aistock_agent.services.trend_reversal import detect_trend_reversal


def _bars(n, *, last_close, last_open, last_high, last_low, amount=150.0):
    closes, opens, highs, lows, amounts = [], [], [], [], []
    base = 3000.0
    for i in range(n):
        c = base + i
        closes.append(c)
        opens.append(c - 1)
        highs.append(c + 2)
        lows.append(c - 2)
        amounts.append(120.0)
    # 末根改为放量阴线
    closes[-1] = last_close
    opens[-1] = last_open
    highs[-1] = last_high
    lows[-1] = last_low
    amounts[-1] = amount
    return closes, opens, highs, lows, amounts


def test_insufficient_below_22_bars():
    out = detect_trend_reversal(*_bars(21, last_close=100, last_open=101, last_high=102, last_low=99))
    assert out["insufficient"] is True and out["confirmed"] is False


def test_no_bearish_volume_bar_not_confirmed():
    # 末根不是放量阴线（放量但阳线）→ 无前置事件
    out = detect_trend_reversal(*_bars(30, last_close=101, last_open=100, last_high=103, last_low=99, amount=150.0))
    assert out["confirmed"] is False


def test_bearish_volume_without_structure_not_confirmed():
    # 末根放量阴线，但此前结构单调下跌（swing 不抬高）
    closes, opens, highs, lows, amounts = [], [], [], [], []
    base = 3200.0
    for i in range(30):
        c = base - i  # 严格下跌
        closes.append(c)
        opens.append(c - 1)
        highs.append(c + 1)
        lows.append(c - 2)
        amounts.append(120.0)
    closes[-1], opens[-1], amounts[-1], highs[-1], lows[-1] = 3170.0, 3190.0, 160.0, 3175.0, 3168.0
    out = detect_trend_reversal(closes, opens, highs, lows, amounts)
    assert out["confirmed"] is False


def test_trend_reversal_confirmed_after_bearish_volume_with_rising_swings():
    """正向用例：前 20 根爬升 + 放量阴线（观察起点）+ 其后 4 周期锯齿向上，
    使观察起点之后最近两个 swing low / swing high 依次抬高（增量 ≥ SWING_MIN_DELTA）→ confirmed。"""
    closes, opens, highs, lows, amounts = [], [], [], [], []
    for i in range(20):
        c = 3000.0 + i * 2
        closes.append(c)
        opens.append(c - 1)
        highs.append(c + 2)
        lows.append(c - 2)
        amounts.append(120.0)
    tail = [3080, 3150, 3250, 3200, 3150, 3300, 3400, 3350, 3300, 3450,
            3550, 3500, 3450, 3600, 3700, 3650, 3600, 3750, 3850, 3800]
    for c in tail:
        closes.append(c)
        opens.append(c - 1)
        highs.append(c + 2)
        lows.append(c - 2)
        amounts.append(120.0)
    # 观察起点（全序列 idx=20）改为放量阴线
    closes[20], opens[20], highs[20], lows[20], amounts[20] = 3080.0, 3200.0, 3220.0, 3060.0, 200.0
    out = detect_trend_reversal(closes, opens, highs, lows, amounts)
    assert out["insufficient"] is False and out["confirmed"] is True
