"""节奏合成引擎（spec §3.1/§5/§7.1/§19）——量纲/映射/档位/阶段/分支全部确定性。"""

import pytest

from aistock_agent.services import rhythm_engine
from aistock_agent.services.rhythm_engine import (
    build_event_branch,
    build_event_hint,
    build_next_event_anchor,
    build_technical_branches,
    compose_score,
    detect_phase,
    fear_greed_anchor,
    map_bipolar,
    project_event_window,
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


def test_position_bands_constants():
    assert "6~8 成" in rhythm_engine.POSITION_BANDS["active"]["text"]
    assert "减仓" in rhythm_engine.POSITION_BANDS["euphoria"]["text"]


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


def test_technical_branches_empty_when_highs_lows_missing():
    closes = [float(i) for i in range(3000, 3400, 10)]
    missing: list[str] = []
    branches = build_technical_branches(
        closes=closes,
        highs=[None] * len(closes),
        lows=[None] * len(closes),
        amounts=[100.0] * len(closes),
        data_missing=missing,
    )
    assert branches == []
    assert any("high/low" in m or "缺失" in m for m in missing)


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


def test_technical_branches_range_anchored_on_trigger() -> None:
    """分支目标区间锚定触发值（design-debate A1 裁决）：
    bullish 区间下界=压力位、bearish 区间上界=支撑位、neutral=真实通道 [S, P]；
    三档区间互斥、触发值=区间边界，消除"站上 P 却给 P±0.5% 对称带"的语义错位。"""
    closes = [float(3000 + (i % 7)) for i in range(30)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    branches = build_technical_branches(closes=closes, highs=highs, lows=lows, amounts=[])
    by_dir = {b["conclusion"]["direction"]: b for b in branches}
    assert {"bullish", "bearish", "neutral"} <= set(by_dir)
    ma20 = sum(closes[-20:]) / 20
    support = max(min(lows[-20:]), ma20 * 0.97)
    pressure = min(max(highs[-20:]), ma20 * 1.03)

    bull_lo, bull_hi = (float(x) for x in by_dir["bullish"]["conclusion"]["range"].split("-"))
    bear_lo, bear_hi = (float(x) for x in by_dir["bearish"]["conclusion"]["range"].split("-"))
    neut_lo, neut_hi = (float(x) for x in by_dir["neutral"]["conclusion"]["range"].split("-"))
    # 触发值 = 区间边界
    assert abs(bull_lo - pressure) < 0.01
    assert abs(bear_hi - support) < 0.01
    assert abs(neut_lo - support) < 0.01
    assert abs(neut_hi - pressure) < 0.01
    # 三档互斥：bull 从压力向上、bear 到支撑向下、neutral 为真实通道
    assert bull_lo >= neut_hi - 0.01
    assert bear_hi <= neut_lo + 0.01
    # 突破后空间（Δ）与通道宽度成比例，非固定 5%（避免过度承诺）
    assert bull_hi > bull_lo and bear_lo < bear_hi


def test_technical_branches_use_dense_support_pressure() -> None:
    """Task2：dense_support/dense_pressure 接入后，分支支撑/压力=密集触碰带，而非旧极值法。

    传 dense_support=3900.0, dense_pressure=4010.0：neutral 区间 = 真实密集带，
    且 bullish 突破区间下界=压力位、bearish 下破区间上界=支撑位（触发值=区间边界），
    点位 fallback 分支的 condition.lo/hi 亦使用密集带值。
    """
    closes = [float(3000 + (i % 7)) for i in range(30)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    branches = build_technical_branches(
        closes=closes, highs=highs, lows=lows, amounts=[],
        dense_support=3900.0, dense_pressure=4010.0,
    )
    by_dir = {b["conclusion"]["direction"]: b for b in branches}
    assert {"bullish", "bearish", "neutral"} <= set(by_dir)
    # neutral = 密集触碰带区间 [S, P]
    assert by_dir["neutral"]["conclusion"]["range"] == "3900.00-4010.00"
    # bullish/bearish 触发值=区间边界（锚定密集带）
    assert by_dir["bullish"]["conclusion"]["range"].split("-")[0] == "4010.00"
    assert by_dir["bearish"]["conclusion"]["range"].split("-")[-1] == "3900.00"
    # 点位 fallback 分支的 condition.lo/hi 也使用密集带（支撑/压力用户可见）
    assert by_dir["bullish"]["condition"]["lo"] == 4010.0
    assert by_dir["bearish"]["condition"]["hi"] == 3900.0
    assert by_dir["neutral"]["condition"]["lo"] == 3900.0
    assert by_dir["neutral"]["condition"]["hi"] == 4010.0


def test_technical_branches_fallback_without_dense() -> None:
    """Task2：未提供 dense 参数时回退旧极值法（兜底），行为与历史一致。"""
    closes = [float(3000 + (i % 7)) for i in range(30)]
    highs = [c + 5.0 for c in closes]
    lows = [c - 5.0 for c in closes]
    branches = build_technical_branches(closes=closes, highs=highs, lows=lows, amounts=[])
    ma20 = sum(closes[-20:]) / 20
    support = max(min(lows[-20:]), ma20 * 0.97)
    pressure = min(max(highs[-20:]), ma20 * 1.03)
    by_dir = {b["conclusion"]["direction"]: b for b in branches}
    assert by_dir["neutral"]["conclusion"]["range"] == f"{support:.2f}-{pressure:.2f}"


def test_event_branch_enum_three_partitions() -> None:
    event = {"date": "2026-09-02", "title": "英伟达财报", "importance": "high", "source": "L3"}
    branches = build_event_branch(event)
    assert len(branches) == 3
    assert all(b["condition"]["kind"] == "enum" for b in branches)
    assert all(b["conclusion"]["validity"] == 5 for b in branches)
    values = {b["condition"]["value"] for b in branches}
    assert values == {"超预期", "符合", "不及预期"}
    by_value = {b["condition"]["value"]: b for b in branches}
    assert by_value["超预期"]["conclusion"]["direction"] == "bullish"
    assert by_value["超预期"]["position_action"]["direction"] == "add"
    assert by_value["超预期"]["anchor"]["threshold"] == "超预期 -> 站上"
    assert by_value["符合"]["conclusion"]["direction"] == "neutral"
    assert by_value["符合"]["position_action"]["direction"] == "hold"
    assert by_value["不及预期"]["conclusion"]["direction"] == "bearish"
    assert by_value["不及预期"]["position_action"]["direction"] == "reduce"
    assert by_value["不及预期"]["anchor"]["threshold"] == "不及预期 -> 跌破"
    assert all(b["conclusion"]["range"] == "" for b in branches)
    assert all("met" not in b for b in branches)
    assert all(b["event_ref"]["title"] == "英伟达财报" for b in branches)
    assert all(b["condition"]["indicator"] == "英伟达财报预期差" for b in branches)


def test_event_branch_non_high_returns_empty() -> None:
    event = {"date": "2026-09-02", "title": "普通事件", "importance": "low"}
    assert build_event_branch(event) == []


def test_detect_phase_arbitration_table_unchanged() -> None:
    # 死码清理后 detect_phase 仍可用（tech 参数保留向后兼容，None=不启用）
    history = [10.0, 20.0, 30.0, 40.0, 50.0]
    p1, _ = detect_phase(history=history, consecutive_ice=0, volume_weak=None, prev_phase=None)
    p2, _ = detect_phase(
        history=history, consecutive_ice=0, volume_weak=None, prev_phase=None, tech=None
    )
    assert p1 == p2  # tech=None 零破坏


def test_compose_score_penalty_lowers_level():
    base_score, _ = compose_score(phase="overheat", trend=1.0, fg=60.0,
                                  trend_available=True, fg_available=True)
    penalized, _ = compose_score(phase="overheat", trend=1.0, fg=60.0,
                                 trend_available=True, fg_available=True, penalty=-8.0)
    assert penalized <= base_score
    assert penalized >= 0.0


def test_compose_score_penalty_default_zero_change():
    a, _ = compose_score(phase="overheat", trend=1.0, fg=60.0,
                         trend_available=True, fg_available=True)
    b, _ = compose_score(phase="overheat", trend=1.0, fg=60.0,
                         trend_available=True, fg_available=True, penalty=0.0)
    assert a == b


def test_build_next_event_anchor_none_without_high():
    events = [{"date": "2026-09-01", "title": "低影响", "importance": "low"}]
    assert build_next_event_anchor(events, "2026-08-28") is None
    assert build_next_event_anchor([], "2026-08-28") is None


def test_build_next_event_anchor_takes_first_high_inherited_order():
    # 顺序继承 app-api 三键排序，Python 不重排：取首条 high
    events = [
        {"date": "2026-09-01", "title": "低影响", "importance": "low"},
        {"date": "2026-09-02", "title": "FOMC 议息", "importance": "high"},
        {"date": "2026-09-03", "title": "CPI", "importance": "high"},
    ]
    anchor = build_next_event_anchor(events, "2026-08-28")
    assert anchor is not None
    assert anchor["title"] == "FOMC 议息"
    assert anchor["event_date"] == "2026-09-02"
    assert anchor["days_until"] == 3
    assert anchor["note"] == "3 天后"


def test_build_next_event_anchor_today_tomorrow_notes():
    today = build_next_event_anchor(
        [{"date": "2026-09-01", "title": "X", "importance": "high"}], "2026-09-01"
    )
    assert today["note"] == "今日"
    tomorrow = build_next_event_anchor(
        [{"date": "2026-09-02", "title": "X", "importance": "high"}], "2026-09-01"
    )
    assert tomorrow["note"] == "明日"


def test_build_next_event_anchor_skips_bad_date_event():
    # G6：日期格式异常跳过错该事件，不抛异常；首个合法 high 仍取到
    events = [
        {"date": "bad-date", "title": "异常日期", "importance": "high"},
        {"date": "2026-09-03", "title": "FOMC", "importance": "high"},
    ]
    anchor = build_next_event_anchor(events, "2026-09-01")
    assert anchor is not None and anchor["title"] == "FOMC"


def test_position_band_to_action_bullish():
    band = {"min": 5, "max": 7, "text": "6~8 成，顺势持有"}
    action = rhythm_engine.position_band_to_action(band, "bullish")
    assert action["direction"] == "add"
    assert action["change"] == "+2 成"
    assert action["band"] == band


def test_position_band_to_action_bearish():
    band = {"min": 0, "max": 3, "text": "轻仓~观望"}
    action = rhythm_engine.position_band_to_action(band, "bearish")
    assert action["direction"] == "reduce"
    assert action["change"] == "-1 成"


def test_position_band_to_action_neutral():
    band = {"min": 5, "max": 6, "text": "半仓~6 成，中性偏多"}
    action = rhythm_engine.position_band_to_action(band, "neutral")
    assert action["direction"] == "hold"
    assert action["change"] == "持仓不变"


def test_qian_yuan_to_yi_constant_exposed():
    from aistock_agent.services.rhythm_engine import QIAN_YUAN_TO_YI

    assert QIAN_YUAN_TO_YI == 1e-5


def test_trend_anchor_zero_amounts_has_no_volume_bias():
    """avg20=0（量能不可用）时不得伪装"缩量 -0.5"：均线满锚 +1.5，量能不加不减。"""
    from aistock_agent.services.rhythm_engine import trend_anchor

    closes = [float(i) for i in range(1, 22)]  # 单边上升 → 满锚 1.5（1.0 均线 + 0.5 位置）
    assert trend_anchor(closes, [0.0] * 21) == pytest.approx(1.5)


def test_next_event_anchor_prefers_high_then_medium() -> None:
    medium = {"date": "2026-09-18", "title": "2026-09 股指期货交割日",
              "importance": "medium", "source": "L1", "type": "delivery"}
    high = {"date": "2026-09-21", "title": "美联储议息", "importance": "high",
            "source": "L3", "type": "macro"}
    only_medium = build_next_event_anchor([medium], "2026-09-17")
    assert only_medium is not None
    assert only_medium["importance"] == "medium"
    assert only_medium["note"] == "明日"
    with_high = build_next_event_anchor([medium, high], "2026-09-17")
    assert with_high is not None and with_high["importance"] == "high"
    assert with_high["title"] == "美联储议息"
    assert build_next_event_anchor([], "2026-09-17") is None


def test_build_event_hint_graded_by_importance() -> None:
    assert build_event_hint(None) == ""
    high = {"title": "美联储议息", "event_date": "2026-09-21", "note": "2 天后",
            "days_until": 2, "importance": "high"}
    medium = {"title": "2026-09 股指期货交割日", "event_date": "2026-09-18",
              "note": "明日", "days_until": 1, "importance": "medium"}
    assert "注意确定性风险" in build_event_hint(high)
    assert "不改仓位倾向" in build_event_hint(medium)


def test_project_event_window_contract_keys() -> None:
    events = [
        {"date": "2026-09-18", "type": "delivery", "title": "2026-09 股指期货交割日",
         "importance": "medium", "source": "L1", "event_time": None, "result": None},
        {"date": "2026-09-17", "title": "无类型事件", "importance": "high"},
    ]
    out = project_event_window(events)
    assert out[0] == {"date": "2026-09-18", "type": "delivery",
                      "title": "2026-09 股指期货交割日", "importance": "medium"}
    assert out[1]["type"] == "seed"
    assert set(out[1].keys()) == {"date", "type", "title", "importance"}


def test_project_event_window_importance_min_and_limit() -> None:
    """展示窗过滤：importance_min 丢弃 low 噪音（G1），limit 防列表爆炸；缺省不过滤。"""
    events = [
        {"date": "2026-09-20", "type": "macro", "title": "CPI 数据公布", "importance": "low"},
        {"date": "2026-09-22", "type": "earnings", "title": "某公司财报", "importance": "medium"},
        {"date": "2026-09-25", "type": "macro", "title": "FOMC 议息", "importance": "high"},
    ]
    assert [e["title"] for e in project_event_window(events)] == ["CPI 数据公布", "某公司财报", "FOMC 议息"]
    filtered = project_event_window(events, importance_min="medium")
    assert [e["title"] for e in filtered] == ["某公司财报", "FOMC 议息"]
    assert [e["importance"] for e in project_event_window(events, limit=2)] == ["low", "medium"]
    # 过滤 + 上限叠加
    assert [e["title"] for e in project_event_window(events, importance_min="high", limit=5)] == ["FOMC 议息"]
    # 缺省 importance → 按 medium 参与排序（不过滤时不丢弃）
    assert [e["title"] for e in project_event_window([{"date": "2026-09-20", "title": "无 importance"}])] == ["无 importance"]
