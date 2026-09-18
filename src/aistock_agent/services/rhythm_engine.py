"""节奏合成引擎（spec §3.1/§5/§7.1/§19）——确定性规则纯函数，无 IO。

量纲口径（§3.1/G17）：参与合成因子先映射 [0,1] 再加权 ×100。
- 双极性锚固定映射 f(x)=(x+2)/4（-2→0、0→0.5、+2→1），禁止 min-max 漂移。
- 节奏分 = 情绪系数×0.60 + 趋势锚×0.27 + 恐贪锚×0.13（事件维移出合成，D10）。
- 缺失因子降权：余下权重按比例重归一（§10）。
- 点位纪律（G19）：本模块按当日行情确定性计算分支点位；LLM 叙事层禁输出点位。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal

from aistock_agent.utils.date import trading_days_between

Phase = Literal["ice", "warm_up", "overheat", "ebb"]
Level = Literal["ice", "low", "normal", "active", "euphoria"]

# spec §5.2.1 T3 / §5.4.1 T4
POSITION_LADDER: list[str] = [
    "空仓观望", "轻仓~三成", "五成~六成", "七成~八成", "八成~满仓",
]
LADDER_MIN, LADDER_MAX = 0, 4
EVENT_ADJ_PRE, EVENT_WATCH_D, EVENT_NEAR_D, EVENT_BRANCH_MAX_D = 1, 3, 2, 3
VOL_UP_RATIO, VOL_DOWN_RATIO = 1.2, 0.8
SWING_WINDOW, SWING_LEFT_RIGHT, SWING_MIN_DELTA, SWING_CONFIRM_BARS = 20, 2, 0.001, 2
MIN_BARS_FOR_REVERSAL = SWING_WINDOW + SWING_CONFIRM_BARS  # 22

# Tushare index_daily amount 单位=千元；engine/前端成交额分支按"亿元"计（1 亿 = 1e5 千元）。
# 单点常量（G6）：任何千元→亿元换算一律引用本常量，禁止散落字面量 1e-5。
QIAN_YUAN_TO_YI: float = 1e-5

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
        # avg20<=0 = 量能不可用（全缺失/全零），不作量能加减（不得伪装"缩量 -0.5"）。
        if avg20 > 0:
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
    penalty: float = 0.0,
) -> tuple[float, list[str]]:
    """节奏分 0-100 + 缺失标注。缺失因子降权重归一（§10）。

    penalty≤0（顶背离降档）在最后应用；ice 为下界封顶由 level_from_score
    阈值天然提供，不引入独立降档函数（C2）。
    """
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
    score = max(0.0, min(100.0, score + penalty))  # penalty≤0，ice 由 level_from_score 天然封顶
    return score, missing


def level_from_score(score: float) -> Level:
    for name, threshold in _LEVELS:
        if score <= threshold:
            return name
    return "euphoria"


def position_band_to_action(band: dict[str, Any], direction: str) -> dict[str, Any]:
    """按 direction 生成结构化仓位动作。

    direction: bullish→add / bearish→reduce / neutral→hold。
    change 成数按保守区间确定性取值（不靠 LLM 拍脑袋，硬约束 #7）。
    """
    d = "add" if direction == "bullish" else ("reduce" if direction == "bearish" else "hold")
    if d == "add":
        change = "+2 成"
    elif d == "reduce":
        change = "-1 成"
    else:
        change = "持仓不变"
    return {"direction": d, "change": change, "band": band}


def derive_position_text(
    *,
    index_closes: list[float],
    mainline: dict[str, object] | None,
    event_d: int | None,
    event_result: str | None,
) -> str:
    """仓位阶梯文案（spec §5.2.2 合成序，H1 绝对锚定；H11：只作用于 stage→level 之后的文案层）。

    step0 指数趋势定 base（多头 3 / 其余 2 / 破位 0）→ step1 硬闸门（指数
    close<ma20 且 ma5<ma10<ma20，或主线板块破位）可否决一切 +1 → step2 主线
    调整（strong +1 / weak 0 / none 强制 0）→ step3 事件档位（d∈{1,2} 且强主线
    +1；d==0 未落档 → min(base,1)；d==0 已落档 → 按预期差方向相对 base 调档）→
    step4 clamp 后取阶梯文案。
    """
    if len(index_closes) >= 20:
        c = index_closes[-1]
        ma5 = sum(index_closes[-5:]) / 5
        ma10 = sum(index_closes[-10:]) / 10
        ma20 = sum(index_closes[-20:]) / 20
        bull = c > ma5 > ma10 > ma20
        gate_hit = c < ma20 and ma5 < ma10 < ma20
    else:
        bull = False
        gate_hit = False
    ml_breakdown = bool((mainline or {}).get("breakdown"))
    if gate_hit or ml_breakdown:
        base = 0
    elif bull:
        base = 3
    else:
        base = 2
    # step1 硬闸门：命中 → base=0 且禁止 +1；数据不足（<20 根）→ 闸门状态未知 → 禁止 +1（H5）
    gate_blocked = gate_hit or ml_breakdown or len(index_closes) < 20
    state = (mainline or {}).get("state")
    strength = (mainline or {}).get("strength")
    adj = 0
    if not gate_blocked:
        # step2 主线调整
        if state == "established" and strength == "strong":
            adj += 1
        elif state == "none":
            base = 0  # 需求①：无清晰主线 → 空仓观望
        # step3 事件档位（仅闸门未禁止时生效）
        if event_d is not None and event_d in (1, 2) \
                and state == "established" and strength == "strong":
            adj += 1
        elif event_d == 0:
            if event_result in EVENT_RESULT_ENUM:
                # 已落档：按事件结果直接定档（EVENT_BAND_KEY 语义，覆盖主线/事件增量）
                direction = EVENT_RESULT_DIRECTION[event_result]
                adj = 1 if direction == "bullish" else (-1 if direction == "bearish" else 0)
            else:
                base = min(base, 1)  # 观望/低仓
    final = base + adj
    # d==0 且未落档 → 最终档位封顶 1（观望/低仓，spec step3）
    if event_d == 0 and event_result not in EVENT_RESULT_ENUM:
        final = min(final, 1)
    final = max(LADDER_MIN, min(LADDER_MAX, final))
    return "建议仓位：" + POSITION_LADDER[final]


def detect_phase(
    *,
    history: list[float],
    consecutive_ice: int,
    volume_weak: bool | None,
    prev_phase: Phase | None,
    slope_window: int = 5,
    slope_threshold: float = 5.0,
    tech: dict[str, object] | None = None,  # 技术佐证输入；None=不启用（原行为）
) -> tuple[Phase | None, dict[str, Any]]:
    """spec §5 判定仲裁表（主信号=温度斜率，佐证=连冰+量能；实验性判定，G3）。

    返回 (phase, evidence)；phase=None 表示判定依据不足且无前阶段。
    tech 非空且数据充分时，主判落空/模糊阶段可被技术佐证覆盖；
    佐证只进 evidence，不产用户可见仓位话术。
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
    # 技术佐证：主判未给明确方向（None）或处于模糊阶段时按技术位覆盖
    if tech and not tech.get("insufficient"):
        if tech.get("below_prior_low") or tech.get("breakdown_ma60"):
            if phase in {None, "warm_up", "overheat"}:
                return "ebb", {"reason": "指数跌破前低/MA60（技术佐证）", "technical": True}
        if tech.get("recovery") and prev_phase in {"ebb", "ice"}:
            return "warm_up", {"reason": "指数站上 MA20（技术佐证）", "technical": True}
    return phase, evidence


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
    dense_support: float | None = None,
    dense_pressure: float | None = None,
    data_missing: list[str] | None = None,
) -> list[dict[str, Any]]:
    """确定性技术点位节点（§19.2，G19：点位由 engine 按当日行情计算）。

    条件分档（§19.5 interval 形态，互斥）：
    - 主用成交额 vs 20 日均量三档（放量/缩量/平量）；
    - amount 缺失 → 退化为指数点位三档（站上压力/跌破支撑/区间内）。
    - high/low 空值兜底（2026-09-05 裁决）：剔除空值行，整体仍不可得则
      返回空 branches 并在 data_missing 留痕，绝不伪造支撑/压力点位。
    """
    if len(closes) < 20:
        return []
    ma20 = sum(closes[-20:]) / 20
    # 密集触碰带优先：需求方核心要求"支撑/压力 = 历史密集触碰带"；未接入时回退旧极值法（兜底）。
    # dense_band 已自行 clamp 到 MA20±clamp_ratio，此处不再重复 clamp，避免二次收窄。
    if dense_support is not None and dense_pressure is not None:
        support = dense_support
        pressure = dense_pressure
    else:
        recent_highs = [h for h in highs[-20:] if h is not None]
        recent_lows = [v for v in lows[-20:] if v is not None]
        if not recent_highs or not recent_lows:
            if data_missing is not None:
                data_missing.append("技术支撑/压力因 high/low 缺失无法计算，本卡不产出生效分支")
            return []
        support = max(min(recent_lows), ma20 * 0.97)
        pressure = min(max(recent_highs), ma20 * 1.03)
    # 突破后空间 Δ = 半通道宽（design-debate A1：range 锚定突破后空间，非固定百分比）
    channel_half = 0.5 * (pressure - support)
    avg20 = 0.0
    amounts_usable = False
    if amounts and len(amounts) >= 20:
        avg20 = sum(amounts[-20:]) / 20
        amounts_usable = avg20 > 0
        if not amounts_usable and data_missing is not None:
            data_missing.append("成交额数据不可用，成交额条件退化为指数点位三档")
    if amounts_usable:
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
                "position_action": position_band_to_action(POSITION_BANDS["active"], "bullish"),
                "anchor": {
                    "metric": "index_close",
                    "threshold": "站稳 +0.5%",
                    "direction": "bullish",
                },
                "touch_strength": None,
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
                "position_action": position_band_to_action(POSITION_BANDS["ice"], "bearish"),
                "anchor": {
                    "metric": "index_close",
                    "threshold": "跌破 -0.5%",
                    "direction": "bearish",
                },
                "touch_strength": None,
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
                "position_action": position_band_to_action(POSITION_BANDS["normal"], "neutral"),
                "anchor": {
                    "metric": "index_close",
                    "threshold": "区间震荡",
                    "direction": "neutral",
                },
                "touch_strength": None,
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
            "position_action": position_band_to_action(POSITION_BANDS["active"], "bullish"),
            "anchor": {
                "metric": "index_close",
                "threshold": "站稳 +0.5%",
                "direction": "bullish",
            },
            "touch_strength": None,
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
            "position_action": position_band_to_action(POSITION_BANDS["ice"], "bearish"),
            "anchor": {
                "metric": "index_close",
                "threshold": "跌破 -0.5%",
                "direction": "bearish",
            },
            "touch_strength": None,
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
            "position_action": position_band_to_action(POSITION_BANDS["normal"], "neutral"),
            "anchor": {
                "metric": "index_close",
                "threshold": "区间震荡",
                "direction": "neutral",
            },
            "touch_strength": None,
            "conclusion": {
                "direction": "neutral",
                "range": f"{support:.2f}-{pressure:.2f}",
                "validity": 5,
                "note": "区间震荡",
            },
        },
    ]


EVENT_RESULT_ENUM = ("超预期", "符合", "不及预期")
EVENT_RESULT_DIRECTION = {"超预期": "bullish", "符合": "neutral", "不及预期": "bearish"}
EVENT_BAND_KEY = {"超预期": "active", "符合": "normal", "不及预期": "ice"}
EVENT_ANCHOR_THRESHOLD = {
    "超预期": "超预期 -> 站上",
    "符合": "符合 -> 震荡",
    "不及预期": "不及预期 -> 跌破",
}


def build_event_branch(
    event: dict[str, Any], origin_date: str | None = None
) -> list[dict[str, Any]]:
    """事件节点（spec §19.2/D10/D15 + 本 spec §5.4/G12）：枚举分档（预期差）。

    只对 high 级事件生成 3 条互斥情景；公布后由确定性 d 逻辑按预期差落档。

    origin_date 提供时启用 d 约束：交易日差 d > EVENT_BRANCH_MAX_D 的事件不产分支
    （避免「闸门说无影响 vs 分支说超预期加仓」的同卡矛盾，spec §5.5.2）。
    返回 [] 表示非 high 事件（无事件分支）或 d 超限（同语义不产分支）。
    """
    if event.get("importance") != "high":
        return []
    if origin_date is not None:
        try:
            d = trading_days_between(date.fromisoformat(origin_date),
                                     date.fromisoformat(str(event.get("date") or "")))
        except ValueError:
            d = 0
        if d is None or d > EVENT_BRANCH_MAX_D:
            return []
    title = str(event.get("title", "关键事件"))
    note_suffix = "（该情景为条件态，不改变主档位）"
    branches: list[dict[str, Any]] = []
    for value in EVENT_RESULT_ENUM:
        direction = EVENT_RESULT_DIRECTION[value]
        band_key = EVENT_BAND_KEY[value]
        branches.append(
            {
                "condition": {
                    "kind": "enum",
                    "indicator": f"{title}预期差",
                    "value": value,
                    "label": value,
                },
                "position_action": position_band_to_action(POSITION_BANDS[band_key], direction),
                "anchor": {
                    "metric": "index_close",
                    "threshold": EVENT_ANCHOR_THRESHOLD[value],
                    "direction": direction,
                },
                "touch_strength": None,
                "conclusion": {
                    "direction": direction,
                    "range": "",
                    "validity": 5,
                    "note": "结果待公布，公布后按预期差落档" + note_suffix,
                },
                "event_ref": {"event_date": str(event.get("date", "")), "title": title},
                "met": None,
            }
        )
    return branches


def build_next_event_anchor(
    events: list[dict[str, object]], origin_date: str
) -> dict[str, object] | None:
    """下一重大事件锚点（design-debate P1，2026-09-02）。

    取窗口内最近事件（high 优先，无 high 退 medium）；同级内顺序继承 app-api
    事件日历下发顺序，Python 侧不重排；N = event_date 与 origin_date **交易日差**
    （D7，spec §5.5.2）。

    原点由调用方给出：节奏大师传**目标交易日**（该卡所描述的那一天，盘前/午间档
    即当天、收盘基准档为次一交易日），使「距今天数」相对卡片描述的那一天。
    注意卡片 `basis_date` 自 2026-09-14 起表示**证据日**（K 线末日），不可用作本处原点。

    A2 双通道：本函数属**展示/提示通道**，故放开 medium；返回值的 `importance`
    供 `build_event_hint` 分级，档位通道（`event_d`/`event_result`）仍只认 high。
    无 high/medium 事件返回 None（前端整块不渲染，对齐空串先例 §7.1）。
    日期解析失败跳过错该事件（G6 不抛异常纪律）；越年交易日差不可算（None）
    同语义跳过，不抛异常。
    """
    for wanted in ("high", "medium"):
        for e in events:
            if e.get("importance") != wanted:
                continue
            event_date = str(e.get("date") or "")
            title = str(e.get("title") or "")
            if not event_date or not title:
                continue
            try:
                # 交易日差（(origin, event_date] 内交易日数）；origin==event_date→0（"今日"）
                days_until = trading_days_between(date.fromisoformat(origin_date),
                                                  date.fromisoformat(event_date))
            except ValueError:
                continue  # 日期格式异常：跳过错该事件，不抛异常穿透
            if days_until is None:
                continue  # 越年/非法 → 与坏日期同语义：跳过错该事件（不抛异常）
            note = ("今日" if days_until == 0
                    else ("明日" if days_until == 1 else f"{days_until} 天后"))
            return {"title": title, "event_date": event_date,
                    "days_until": days_until, "note": note, "importance": wanted}
    return None


_EVENT_TYPE_FALLBACK = "seed"
_EVENT_TYPES = ("delivery", "earnings", "seed", "macro")


def project_event_window(events: list[dict[str, object]]) -> list[dict[str, object]]:
    """卡片事件日历投影（spec §5.2）：`{date,type,title,importance}` 四键。

    前端 `RhythmEvent` 契约只认这四键（`RhythmCard.vue` 事件日历块）；`type` 缺失
    或非法 → 落 `seed`（前端 eventTypeLabel 有该映射），不返回 null。
    """
    out: list[dict[str, object]] = []
    for e in events:
        title = str(e.get("title") or "")
        if not title:
            continue
        etype = str(e.get("type") or "")
        if etype not in _EVENT_TYPES:
            etype = _EVENT_TYPE_FALLBACK
        out.append({
            "date": str(e.get("date") or ""),
            "type": etype,
            "title": title,
            "importance": str(e.get("importance") or "medium"),
        })
    return out


def build_event_hint(anchor: dict[str, object] | None) -> str:
    """事件临近提示（spec §5.1）：按 importance 分级，只提示不改数值。"""
    if not anchor:
        return ""
    head = (
        f"{anchor.get('title')}（{anchor.get('event_date')}，"
        f"{anchor.get('note')}）："
    )
    if anchor.get("importance") == "high":
        return head + "事件临近，注意确定性风险"
    return head + "事件临近，注意事件扰动（不改仓位倾向）"
