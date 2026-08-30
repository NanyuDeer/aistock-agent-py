"""节奏合成引擎（spec §3.1/§5/§7.1/§19）——量纲/映射/档位/阶段/分支全部确定性。"""
from aistock_agent.services.rhythm_engine import (
    DISCLAIMER,
    build_event_branch,
    build_technical_branches,
    compose_score,
    detect_conflict,
    detect_phase,
    fear_greed_anchor,
    level_from_score,
    map_bipolar,
    position_band,
    sentiment_coefficient,
    trend_anchor,
)


def test_map_bipolar_fixed_anchors() -> None:
    assert map_bipolar(-2.0) == 0.0
    assert map_bipolar(0.0) == 0.5
    assert map_bipolar(2.0) == 1.0
    assert 0.0 <= map_bipolar(10.0) <= 1.0


def test_sentiment_coefficient_table() -> None:
    assert sentiment_coefficient("ice") == 0.15
    assert sentiment_coefficient("warm_up") == 0.45
    assert sentiment_coefficient("overheat") == 0.85
    assert sentiment_coefficient("ebb") == 0.35
    assert sentiment_coefficient(None) == 0.5


def test_fear_greed_anchor_bands() -> None:
    assert fear_greed_anchor(90.0) == 1.6
    assert fear_greed_anchor(70.0) == 0.8
    assert fear_greed_anchor(50.0) == 0.0
    assert fear_greed_anchor(10.0) == -1.6


def test_compose_score_weighted_and_missing_renorm() -> None:
    score, missing = compose_score(
        phase="warm_up", trend=0.0, fg=0.0, trend_available=True, fg_available=True
    )
    assert 0.0 <= score <= 100.0
    # 中性三因子 → 0.5*100 = 50（warm_up 0.45 略低 → 约 47-49 区间，仅断言量纲）
    assert score >= 40.0
    # 缺失恐贪 → 余下权重重归一（0.60/0.27 → 0.69/0.31），不 fail
    score2, missing2 = compose_score(
        phase="warm_up", trend=0.0, fg=None, trend_available=True, fg_available=False
    )
    assert "恐贪数据缺失" in missing2
    assert 0.0 <= score2 <= 100.0


def test_level_and_position_band() -> None:
    assert level_from_score(10.0) == "ice"
    assert level_from_score(30.0) == "low"
    assert level_from_score(50.0) == "normal"
    assert level_from_score(70.0) == "active"
    assert level_from_score(90.0) == "euphoria"
    band = position_band("active")
    assert band["text"] == "6~8 成，顺势持有"
    assert "减仓" in position_band("euphoria")["text"]


def test_trend_anchor_ma_alignment() -> None:
    # 单边上升收盘价 → 正锚
    closes = [float(3000 + i * 2) for i in range(30)]
    amounts = [100.0] * 30
    assert trend_anchor(closes, amounts) == 2.0
    # 数据不足 → None（缺失语义）
    assert trend_anchor([1.0, 2.0], [1.0]) is None


def test_detect_phase_arbitration_table() -> None:
    # 升 → 升温
    history = [30.0, 32.0, 34.0, 36.0, 42.0]
    phase, ev = detect_phase(history=history, consecutive_ice=0, volume_weak=None, prev_phase=None)
    assert phase == "warm_up" and ev["reason"] == "温度上行"
    # 降 → 退潮
    phase, _ = detect_phase(
        history=[50.0, 46.0, 42.0, 40.0, 36.0], consecutive_ice=0, volume_weak=None, prev_phase=None
    )
    assert phase == "ebb"
    # 平 + 连冰 ≥2 → 冰点
    phase, _ = detect_phase(
        history=[18.0, 18.0, 19.0, 18.0, 18.0], consecutive_ice=2, volume_weak=None, prev_phase=None
    )
    assert phase == "ice"
    # 平 + 弱量能 → 退潮
    phase, _ = detect_phase(
        history=[40.0, 40.0, 41.0, 40.0, 40.0], consecutive_ice=0, volume_weak=True, prev_phase=None
    )
    assert phase == "ebb"
    # 平 + 无佐证 + 无前阶段 → None（判定依据不足）
    phase, ev = detect_phase(
        history=[40.0, 40.0, 41.0, 40.0, 40.0],
        consecutive_ice=0,
        volume_weak=False,
        prev_phase=None,
    )
    assert phase is None and ev.get("evidence_insufficient")
    # 平 + 无佐证 + 前阶段 → 沿用
    phase, _ = detect_phase(
        history=[40.0, 40.0, 41.0, 40.0, 40.0],
        consecutive_ice=0,
        volume_weak=False,
        prev_phase="ebb",
    )
    assert phase == "ebb"


def test_detect_conflict() -> None:
    # 趋势强多 + 情绪冰点 → 背离
    assert detect_conflict(phase="ice", trend=2.0)[0] is True
    # 趋势强空 + 情绪过热 → 背离
    assert detect_conflict(phase="overheat", trend=-2.0)[0] is True
    # 一致 → 无背离
    assert detect_conflict(phase="warm_up", trend=1.0)[0] is False


def test_technical_branches_turnover_condition() -> None:
    closes = [float(3000 + (i % 7)) for i in range(30)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    amounts = [100.0] * 30
    branches = build_technical_branches(closes=closes, highs=highs, lows=lows, amounts=amounts)
    assert 2 <= len(branches) <= 3
    kinds = [b["condition"]["indicator"] for b in branches]
    assert set(kinds) == {"成交额"}
    # §19.5 interval 互斥：同 indicator 区间不重叠
    lo_hi = [(b["condition"].get("lo"), b["condition"].get("hi")) for b in branches]
    for i in range(len(lo_hi)):
        for j in range(i + 1, len(lo_hi)):
            a_lo, a_hi = lo_hi[i]
            b_lo, b_hi = lo_hi[j]
            if a_lo is not None and b_hi is not None and a_lo < b_hi:
                assert False, "区间重叠"
            if b_lo is not None and a_hi is not None and b_lo < a_hi:
                assert False, "区间重叠"


def test_technical_branches_price_fallback() -> None:
    closes = [float(3000 + (i % 7)) for i in range(30)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    branches = build_technical_branches(closes=closes, highs=highs, lows=lows, amounts=[])
    assert len(branches) >= 2
    assert all(b["condition"]["indicator"] == "上证指数点位" for b in branches)
    # 结论 range 必为区间形态（engine 确定性点位，G19）
    assert all("" in b["conclusion"]["range"] and "-" in b["conclusion"]["range"] for b in branches)


def test_event_branch_enum_three_partitions() -> None:
    event = {"date": "2026-09-02", "title": "英伟达财报", "importance": "high", "source": "L3"}
    branch = build_event_branch(event)
    assert branch is not None
    assert branch["condition"]["kind"] == "enum"
    assert branch["condition"]["value"] in {"超预期", "符合", "不及预期"}
    assert branch["conclusion"]["direction"] in {"bullish", "bearish", "neutral"}
    assert branch["conclusion"]["validity"] == 5


def test_disclaimer_present() -> None:
    assert "不构成任何投资建议" in DISCLAIMER
