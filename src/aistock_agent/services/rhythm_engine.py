"""节奏合成引擎（spec §3.1/§5/§7.1/§19）——确定性规则纯函数，无 IO。

量纲口径（§3.1/G17）：参与合成因子先映射 [0,1] 再加权 ×100。
- 双极性锚固定映射 f(x)=(x+2)/4（-2→0、0→0.5、+2→1），禁止 min-max 漂移。
- 节奏分 = 情绪系数×0.60 + 趋势锚×0.27 + 恐贪锚×0.13（事件维移出合成，D10）。
- 缺失因子降权：余下权重按比例重归一（§10）。
- 点位纪律（G19）：本模块按当日行情确定性计算分支点位；LLM 叙事层禁输出点位。
"""
from __future__ import annotations

from typing import Any, Literal

Phase = Literal["ice", "warm_up", "overheat", "ebb"]
Level = Literal["ice", "low", "normal", "active", "euphoria"]

DISCLAIMER = "本页内容为研究参考，不构成任何投资建议，据此操作风险自担。"

WEIGHTS: dict[str, float] = {"sentiment": 0.60, "trend": 0.27, "fear_greed": 0.13}

SENTIMENT_COEFF: dict[str, float] = {
    "ice": 0.15,
    "warm_up": 0.45,
    "overheat": 0.85,
    "ebb": 0.35,
    "missing": 0.5,
}

FG_ANCHORS: list[tuple[float, float]] = [
    (80.0, 1.6), (60.0, 0.8), (40.0, 0.0), (20.0, -0.8), (float("-inf"), -1.6),
]

_LEVELS: list[tuple[Level, float]] = [
    ("ice", 20.0), ("low", 45.0), ("normal", 55.0), ("active", 80.0), ("euphoria", float("inf")),
]

POSITION_BANDS: dict[str, dict[str, Any]] = {
    "euphoria": {"text": "半仓~减仓，防退潮回落"},
    "active": {"text": "6~8 成，顺势持有"},
    "normal": {"text": "半仓~6 成，中性偏多"},
    "low": {"text": "轻仓~观望"},
    "ice": {"text": "空仓~观察（冰点修复窗口属博弈，非确定性加仓信号）"},
}


def map_bipolar(anchor: float) -> float:
    """双极性锚固定映射（§3.1）。"""
    return max(0.0, min(1.0, (anchor + 2.0) / 4.0))


def sentiment_coefficient(phase: Phase | None) -> float:
    return SENTIMENT_COEFF.get(phase or "missing", 0.5)


def fear_greed_anchor(index: float | None) -> float:
    """恐贪 0-100 → 锚值（§3.1 映射表）。缺失由 compose_score 降权处理。"""
    if index is None:
        return 0.0
    for low, anchor in FG_ANCHORS:
        if index >= low:
            return anchor
    return -1.6


def trend_anchor(closes: list[float], amounts: list[float]) -> float | None:
    """均线多空 + 量能打分（-2..+2）。closes/amounts 按时间升序；数据不足 → None。"""
    if len(closes) < 21:
        return None
    c = closes[-1]
    ma5 = sum(closes[-5:]) / 5
    ma10 = sum(closes[-10:]) / 10
    ma20 = sum(closes[-20:]) / 20
    score = 0.0
    if c > ma5 > ma10 > ma20:
        score += 1.0
    elif c < ma5 < ma10 < ma20:
        score -= 1.0
    score += 0.5 if c > ma20 else -0.5
    if amounts and len(amounts) >= 20:
        avg20 = sum(amounts[-20:]) / 20
        avg5 = sum(amounts[-5:]) / 5
        # 量能健康（放量或平量）视为趋势确认 +0.5；仅缩量（<20日均量80%）-0.5。
        # 测试口径：单边上升+平量 → 满锚 2.0（test_trend_anchor_ma_alignment）。
        if avg5 >= avg20 * 0.8:
            score += 0.5
        else:
            score -= 0.5
    return max(-2.0, min(2.0, score))


def compose_score(
    *,
    phase: Phase | None,
    trend: float | None,
    fg: float | None,
    trend_available: bool,
    fg_available: bool,
) -> tuple[float, list[str]]:
    """节奏分 0-100 + 缺失标注。缺失因子降权重归一（§10）。"""
    parts: list[tuple[str, float, float]] = [
        ("sentiment", sentiment_coefficient(phase), WEIGHTS["sentiment"])
    ]
    missing: list[str] = []
    if trend is None or not trend_available:
        missing.append("趋势数据缺失")
    else:
        parts.append(("trend", map_bipolar(trend), WEIGHTS["trend"]))
    if fg is None or not fg_available:
        missing.append("恐贪数据缺失")
    else:
        parts.append(("fear_greed", map_bipolar(fg), WEIGHTS["fear_greed"]))
    total_w = sum(w for _, _, w in parts)
    if total_w <= 0:
        return 50.0, missing
    score = sum(v * w for _, v, w in parts) / total_w * 100.0
    return max(0.0, min(100.0, score)), missing


def level_from_score(score: float) -> Level:
    for name, threshold in _LEVELS:
        if score <= threshold:
            return name
    return "euphoria"


def position_band(level: Level) -> dict[str, Any]:
    return dict(POSITION_BANDS[level])


def ma_breadth(
    closes: list[float],
    *,
    arm_days: int = 3,
) -> dict[str, object]:
    """指数技术位多级确认佐证（C1）。MA60 不可算（<65 根）时 insufficient=True。"""
    if len(closes) < 65:
        return {
            "ma20": None, "ma60": None,
            "close": closes[-1] if closes else None,
            "warning": False, "recovery": False,
            "breakdown_ma60": False, "below_prior_low": False,
            "insufficient": True,
        }
    ma20 = sum(closes[-20:]) / 20
    ma60 = sum(closes[-60:]) / 60
    close = closes[-1]
    prior_low = min(closes[-40:-20]) if len(closes) >= 40 else min(closes)
    last3 = closes[-arm_days:]
    return {
        "ma20": ma20, "ma60": ma60, "close": close,
        "warning": close < ma20,
        "recovery": close > ma20 and all(c > ma20 for c in last3),
        "breakdown_ma60": close < ma60 and all(c < ma60 for c in last3),
        "below_prior_low": close < prior_low and all(c < prior_low for c in last3),
        "insufficient": False,
    }


def detect_phase(
    *,
    history: list[float],
    consecutive_ice: int,
    volume_weak: bool | None,
    prev_phase: Phase | None,
    slope_window: int = 5,
    slope_threshold: float = 5.0,
    tech: dict[str, object] | None = None,  # ma_breadth 输出；None=不启用（原行为）
) -> tuple[Phase | None, dict[str, Any]]:
    """spec §5 判定仲裁表（主信号=温度斜率，佐证=连冰+量能；实验性判定，G3）。

    返回 (phase, evidence)；phase=None 表示判定依据不足且无前阶段。
    tech（C1 ma_breadth 输出）非空且数据充分时，主判落空/模糊阶段可被
    技术佐证覆盖；佐证只进 evidence，不产用户可见仓位话术。
    """
    if len(history) < 2:
        return prev_phase, {"reason": "温度序列不足", "evidence_insufficient": True}
    recent = history[-slope_window:]
    slope = recent[-1] - recent[0]
    current = history[-1]
    # 主判收敛为单一 (phase, evidence)，佐证覆盖统一在返回前应用（C1）
    if slope > slope_threshold:
        phase: Phase | None = "warm_up" if prev_phase != "overheat" else "overheat"
        evidence: dict[str, Any] = {"slope": round(slope, 1), "reason": "温度上行"}
    elif slope < -slope_threshold:
        if current <= 20 and consecutive_ice >= 2:
            phase = "ice"
            evidence = {"slope": round(slope, 1), "reason": "温度下行且连冰"}
        else:
            phase = "ebb"
            evidence = {"slope": round(slope, 1), "reason": "温度下行"}
    elif consecutive_ice >= 2:
        phase = "ice"
        evidence = {"slope": round(slope, 1), "reason": "温度低位平 + 连冰"}
    elif volume_weak:
        phase = "ebb"
        evidence = {"slope": round(slope, 1), "reason": "温度平 + 量能偏弱"}
    elif prev_phase is not None:
        phase = prev_phase
        evidence = {"slope": round(slope, 1), "reason": "沿用前阶段（判定依据不足）"}
    else:
        phase = None
        evidence = {
            "slope": round(slope, 1),
            "reason": "判定依据不足（无前阶段）",
            "evidence_insufficient": True,
        }
    # C1 技术佐证：主判未给明确方向（None）或处于模糊阶段时按技术位覆盖
    if tech and not tech.get("insufficient"):
        if tech.get("below_prior_low") or tech.get("breakdown_ma60"):
            if phase in {None, "warm_up", "overheat"}:
                return "ebb", {"reason": "指数跌破前低/MA60（技术佐证）", "technical": True}
        if tech.get("recovery") and prev_phase in {"ebb", "ice"}:
            return "warm_up", {"reason": "指数站上 MA20（技术佐证）", "technical": True}
    return phase, evidence


def detect_conflict(phase: Phase | None, trend: float | None) -> tuple[bool, str]:
    """强信号方向相反并存 → 背离（§7.2/G2）。仲裁优先级：趋势 > 情绪 > 恐贪（D1）。"""
    if trend is None:
        return False, ""
    if trend >= 1.5 and phase in {"ice", "ebb"}:
        return True, "趋势偏多但情绪周期偏冷，信号背离"
    if trend <= -1.5 and phase in {"warm_up", "overheat"}:
        return True, "趋势偏空但情绪周期偏热，信号背离"
    return False, ""


def _range_above(value: float, delta: float) -> str:
    """突破锚定（design-debate A1 裁决）：目标区间 = [触发位, 触发位+Δ]，触发值=区间下界。

    突破后空间 Δ 与通道宽度成比例（0.5×通道宽），而非固定百分比外推——
    避免"站上 P 却给 P±0.5% 对称带"（触发值∉区间）的语义错位与过度承诺。
    """
    return f"{value:.2f}-{value + delta:.2f}"


def _range_below(value: float, delta: float) -> str:
    """跌破锚定（design-debate A1 裁决）：目标区间 = [触发位-Δ, 触发位]，触发值=区间上界。"""
    return f"{value - delta:.2f}-{value:.2f}"


def build_technical_branches(
    *,
    closes: list[float],
    highs: list[float],
    lows: list[float],
    amounts: list[float],
) -> list[dict[str, Any]]:
    """确定性技术点位节点（§19.2，G19：点位由 engine 按当日行情计算）。

    条件分档（§19.5 interval 形态，互斥）：
    - 主用成交额 vs 20 日均量三档（放量/缩量/平量）；
    - amount 缺失 → 退化为指数点位三档（站上压力/跌破支撑/区间内）。
    """
    if len(closes) < 20:
        return []
    ma20 = sum(closes[-20:]) / 20
    recent_high = max(highs[-20:])
    recent_low = min(lows[-20:])
    support = max(recent_low, ma20 * 0.97)
    pressure = min(recent_high, ma20 * 1.03)
    # 突破后空间 Δ = 半通道宽（design-debate A1：range 锚定突破后空间，非固定百分比）
    channel_half = 0.5 * (pressure - support)
    if amounts and len(amounts) >= 20:
        avg20 = sum(amounts[-20:]) / 20
        return [
            {
                "condition": {
                    "kind": "interval",
                    "indicator": "成交额",
                    "lo": round(avg20 * 1.2, 2),
                    "hi": None,
                    "unit": "亿元",
                    "label": f"放量（>{avg20 * 1.2:.0f}亿）",
                },
                "conclusion": {
                    "direction": "bullish",
                    "range": _range_above(pressure, channel_half),
                    "validity": 5,
                    "note": "放量突破压力位",
                },
            },
            {
                "condition": {
                    "kind": "interval",
                    "indicator": "成交额",
                    "lo": None,
                    "hi": round(avg20 * 0.8, 2),
                    "unit": "亿元",
                    "label": f"缩量（<{avg20 * 0.8:.0f}亿）",
                },
                "conclusion": {
                    "direction": "bearish",
                    "range": _range_below(support, channel_half),
                    "validity": 5,
                    "note": "缩量回踩支撑位",
                },
            },
            {
                "condition": {
                    "kind": "interval",
                    "indicator": "成交额",
                    "lo": round(avg20 * 0.8, 2),
                    "hi": round(avg20 * 1.2, 2),
                    "unit": "亿元",
                    "label": "平量",
                },
                "conclusion": {
                    "direction": "neutral",
                    "range": f"{support:.2f}-{pressure:.2f}",
                    "validity": 5,
                    "note": "区间震荡",
                },
            },
        ]
    return [
        {
            "condition": {
                "kind": "interval",
                "indicator": "上证指数点位",
                "lo": round(pressure, 2),
                "hi": None,
                "label": f"收盘站上 {pressure:.0f} 压力位",
            },
            "conclusion": {
                "direction": "bullish",
                "range": _range_above(pressure, channel_half),
                "validity": 5,
                "note": "突破压力位",
            },
        },
        {
            "condition": {
                "kind": "interval",
                "indicator": "上证指数点位",
                "lo": None,
                "hi": round(support, 2),
                "label": f"收盘跌破 {support:.0f} 支撑位",
            },
            "conclusion": {
                "direction": "bearish",
                "range": _range_below(support, channel_half),
                "validity": 5,
                "note": "跌破支撑位",
            },
        },
        {
            "condition": {
                "kind": "interval",
                "indicator": "上证指数点位",
                "lo": round(support, 2),
                "hi": round(pressure, 2),
                "label": "支撑-压力区间内",
            },
            "conclusion": {
                "direction": "neutral",
                "range": f"{support:.2f}-{pressure:.2f}",
                "validity": 5,
                "note": "区间震荡",
            },
        },
    ]


EVENT_RESULT_ENUM = ("超预期", "符合", "不及预期")


def build_event_branch(event: dict[str, Any]) -> dict[str, Any] | None:
    """事件节点（§19.2/D10/D15）：枚举分档（预期差），公布前不预判方向（占位"结果待公布"）。

    只对 high 级事件生成；各档预绑结论（区间点位由 engine 确定性给，公布后按预期差落档触发）。
    """
    if event.get("importance") != "high":
        return None
    title = str(event.get("title", "关键事件"))
    return {
        "condition": {
            "kind": "enum",
            "indicator": f"{title}预期差",
            "value": EVENT_RESULT_ENUM[0],
            "label": "超预期",
        },
        "conclusion": {
            "direction": "bullish",
            "range": "",
            "validity": 5,
            "note": "结果待公布，公布后按预期差落档",
        },
        "event_ref": {"event_date": str(event.get("date", "")), "title": title},
    }
