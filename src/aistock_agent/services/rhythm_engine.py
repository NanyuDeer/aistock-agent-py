"""节奏合成引擎（spec §3.1/§5/§7.1/§19）——确定性规则纯函数，无 IO。

量纲口径（§3.1/G17）：参与合成因子先映射 [0,1] 再加权 ×100。
- 双极性锚固定映射 f(x)=(x+2)/4（-2→0、0→0.5、+2→1），禁止 min-max 漂移。
- 节奏分 = 情绪系数×0.60 + 趋势锚×0.27 + 恐贪锚×0.13（事件维移出合成，D10）。
- 缺失因子降权：余下权重按比例重归一（§10）。
- 点位纪律（G19）：本模块按当日行情确定性计算分支点位；LLM 叙事层禁输出点位。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, cast

from aistock_agent.schemas.rhythm_master import (
    Direction,
    LedgerItem,
    PositionAction,
    PositionLedger,
)
from aistock_agent.utils.date import trading_days_between

Phase = Literal["ice", "warm_up", "overheat", "ebb"]
Level = Literal["ice", "low", "normal", "active", "euphoria"]
# 事件调整的已计入类型（「临近 +1」与「按结果定档」语义不同，盘前处理方式也不同）
EventAppliedKind = Literal["approaching", "result"]

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
    """未接线说明（2026-09-18）：本函数与 `level_from_score` 在 rhythm 链路**无调用点**——
    生产的档位真相源是 `schemas/rhythm_master.STAGE_TO_LEVEL`（由 `stage` 派生）。
    保留原因：08-29 spec §3/§3.1 依据 + 阈值校准（G7/D9）可能复用。
    节奏分 0-100 + 缺失标注。缺失因子降权重归一（§10）。

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


def position_band_to_action(
    band: dict[str, Any],
    direction: str,
    *,
    from_index: int | None = None,
    gate_blocked: bool = False,
) -> dict[str, Any]:
    """按 direction 生成结构化仓位动作，并接到当前档位（因果输出 spec §7）。

    direction: bullish→add（+2 档）/ bearish→reduce（−1 档）/ neutral→hold（维持），
    `to_index = clamp(from_index + delta, LADDER_MIN, LADDER_MAX)`。

    **闸门压制**（spec §7.2）：`gate_blocked and bullish` → `to_index = from_index`、
    `blocked_by_gate = True`、`change = "闸门未解除，暂不加"`。理由：闸门命中时
    `derive_position_index` 会把档位压到 0 并禁止 +1，此时承诺「加仓」是系统自身
    不会执行的动作。

    **边界钳制**：`change` 取**实际兑现步长**（`to_index - from_index`），故 clamp
    生效时不虚报 —— 4 档无法再加、0 档无法再减 → `"维持"`；3 档 bullish 只到 4 档
    → `"+1 档"`（名义 +2）。分支不得写出未兑现的步长。

    `from_index is None`（旧卡 / 未接线调用）→ `from_index` / `to_index` 均为 None、
    `blocked_by_gate=False`，`change` **回退旧静态文案**（「+2 成」/「-1 成」/「持仓不变」），
    保证旧报告与旧调用点行为不变（spec §8）。

    有 from_index 时 `change` 一律以「档」为单位（spec §7.3）：「成」是仓位比例，
    「档」是本系统阶梯序号，混用即 §1.2 矛盾的根源。

    返回的 `band` 服从**实际档位**（spec §11-3）：有 from_index 时取
    `{"text": POSITION_LADDER[to_index]}`，闸门压制/边界钳制后不会与 `to_index` 打架；
    无 from_index（旧卡/旧调用）时原样回传入参 band，行为不变。
    """
    d = "add" if direction == "bullish" else ("reduce" if direction == "bearish" else "hold")
    delta = 2 if direction == "bullish" else (-1 if direction == "bearish" else 0)
    band_out: dict[str, Any] = band
    if from_index is None:
        # 旧静态文案（兼容旧报告/旧测试）：语义已被 §7 取代，仅在无档位可接时使用。
        change = "+2 成" if d == "add" else ("-1 成" if d == "reduce" else "持仓不变")
        to_index: int | None = None
        blocked_by_gate = False
    else:
        blocked_by_gate = bool(gate_blocked and direction == "bullish")
        if blocked_by_gate:
            to_index = from_index
            change = "闸门未解除，暂不加"
        else:
            to_index = max(LADDER_MIN, min(LADDER_MAX, from_index + delta))
            step = to_index - from_index
            if step == 0:
                change = "维持"
            else:
                change = f"+{step} 档" if step > 0 else f"{step} 档"
        # 文案服从实际档位（spec §11-3）：band 是静态表值，闸门/钳制后可能与 to_index 打架
        band_out = {"text": POSITION_LADDER[to_index]}
    return {
        "direction": d,
        "change": change,
        "band": band_out,
        "from_index": from_index,
        "to_index": to_index,
        "blocked_by_gate": blocked_by_gate,
    }


def _mainline_adjustment(state: str | None, strength: object) -> int:
    """step2 主线增量：成立·强 → +1，其余 0（无主线强制 0 由调用方改 base）。

    `event_adjustment` 的事件落档为**绝对定档**（覆盖主线增量），需扣除本增量才能
    换算成相对调整量，故两处共用同一表达式，防判据漂移。
    """
    return 1 if state == "established" and strength == "strong" else 0


# 「d==0 未落档 → 档位封顶 1」的契约常量（`event_adjustment` 的 reason 逐字等于它）。
# 调用方用**精确比较**识别该上界约束，禁止按文案模糊匹配。
EVENT_CAP_REASON = "d=0 事件未落档 → 档位封顶 1"


def event_adjustment(
    *,
    event_d: int | None,
    event_result: str | None,
    mainline_state: str | None,
    mainline_strength: object,
    gate_blocked: bool,
    base_index: int,
) -> tuple[int, str]:
    """step3 事件档位调整量 —— 唯一判据（spec §5.2 / 职责重定义 spec §4.2）。

    返回 `(delta, reason)`：delta 是相对**当前档位** `base_index`（step0-2 已完成，
    即 base + 主线增量）的调整量，调用方一律 `clamp(base_index + delta, 0, 4)`；
    本函数**不做** clamp。

    `d == 0` 未落档的上界约束「封顶 1」**已按 base_index 折算进 delta**
    （`min(base_index, 1) - base_index`，base_index > 1 时为负），因此两个调用方
    （`derive_position_index` 收盘定档 / worker 盘前事件修正）拿到完全一致的语义：
    `base_index + delta` 即封顶后的档位，无需各自再判 `min(idx, 1)`。
    `reason == EVENT_CAP_REASON`（逐字相等）供调用方识别该约束并改写账本文案。

    判据与抽取前的 step3 逐行一致：
    - `d == 0` 且结果未落档 → 封顶 1（该约束与闸门无关，历史行为如此）
    - 闸门命中 / K 线不足 → 0（禁止 +1）
    - `d ∈ {1,2}` 且主线成立·强 → +1
    - `d == 0` 已落档 → 事件结果绝对定档（覆盖主线增量）→ `target - 主线增量`
    """
    if event_d == 0 and event_result not in EVENT_RESULT_ENUM:
        return min(base_index, 1) - base_index, EVENT_CAP_REASON
    if gate_blocked:
        return 0, "闸门命中或数据不足，事件档位不生效"
    if (
        event_d is not None
        and event_d in (1, 2)
        and mainline_state == "established"
        and mainline_strength == "strong"
    ):
        return 1, f"d={event_d} 事件窗口内 + 主线成立·强 → +1"
    if event_d == 0:
        direction = EVENT_RESULT_DIRECTION[cast("str", event_result)]
        target = 1 if direction == "bullish" else (-1 if direction == "bearish" else 0)
        ml_adj = _mainline_adjustment(mainline_state, mainline_strength)
        return target - ml_adj, f"d=0 事件已落档（{event_result}）→ 事件档位 {target:+d}"
    return 0, "无事件档位调整"


def event_applied_kind(
    *,
    event_d: int | None,
    event_result: str | None,
    gate_blocked: bool,
    delta: int,
    reason: str,
) -> EventAppliedKind | None:
    """事件调整的**已计入类型**（幂等闸门，职责重定义 spec §4.1）。

    - 闸门命中 → None（事件未生效）；
    - `d == 0` 已落档 → "result"（含 delta==0：结果确已用于定档，不得按同一条再计）；
    - `d ∈ {1,2}` 且主线强（delta>0）→ "approaching"（临近 +1，结果未知，盘前可撤销重算）；
    - `d == 0` 未落档（封顶）→ None：封顶是「结果未知」的安全约束，不是按结果调过，
      morning 拿到结果后仍须能正确应用。
    """
    if gate_blocked:
        return None
    if event_d == 0 and event_result in EVENT_RESULT_ENUM:
        return "result"
    if delta > 0 and reason != EVENT_CAP_REASON:
        return "approaching"
    return None


def event_ledger_item(
    *,
    event_d: int | None,
    event_result: str | None,
    gate_blocked: bool,
    block_reason: str,
    mainline_state: str | None,
    mainline_strength: object,
) -> LedgerItem:
    """事件账本行（`_build_event_ledger_item` 的唯一公开入口）：文案单一来源。

    盘前事件修正改写账本行时必须复用本函数：同一份账本里不允许出现两套事件表述
    （`value_text` 说「未落档」而 `judgement` 说「+1」）。
    """
    return _build_event_ledger_item(
        event_d=event_d,
        event_result=event_result,
        gate_blocked=gate_blocked,
        block_reason=block_reason,
        state=mainline_state,
        strength=mainline_strength,
    )


def derive_position_index(
    *,
    index_closes: list[float],
    mainline: dict[str, object] | None,
    event_d: int | None,
    event_result: str | None,
    breadth: dict[str, Any] | None = None,
) -> tuple[int, PositionLedger]:
    """仓位档位序号 0-4 + 推导账本（因果输出 spec §4.1，H1 绝对锚定）。

    step0 指数趋势定 base（多头 3 / 其余 2 / 破位 0）→ step1 硬闸门（指数
    close<ma20 且 ma5<ma10<ma20，或主线板块破位）可否决一切 +1 → step2 主线
    调整（strong +1 / weak 0 / none 强制 0）→ step3 事件档位（判据全部委托
    `event_adjustment`：d∈{1,2} 且强主线 +1；d==0 未落档 → 封顶 1；d==0 已落档
    → 按预期差方向覆盖主线增量）→ step4 clamp 后取序号。

    **判据唯一真相源**：本函数是 step0-4 的唯一实现，账本只是把中间量
    （base / adj / gate_hit / mainline_state）结构化，不得在此新增或改动判据。

    breadth 仅用于账本展示（宽度只参与 certainty，**不参与档位判定**），
    不影响返回值中的序号。
    """
    bars = len(index_closes)
    trend_known = bars >= 20
    c: float | None = None
    ma20: float | None = None
    if trend_known:
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
    gate_blocked = gate_hit or ml_breakdown or bars < 20
    # 闭集归一（2026-11-04 裁决）：账本只允许 established / none / unavailable / None，
    # 未知态按「数据不可用」处理，不把原始值透出到卡面。
    state: str | None = None
    raw_state = (mainline or {}).get("state")
    if raw_state in ("established", "none", "unavailable"):
        state = str(raw_state)
    elif raw_state is not None:
        state = "unavailable"
    strength = (mainline or {}).get("strength")
    ml_adj = 0
    if not gate_blocked:
        # step2 主线调整
        ml_adj = _mainline_adjustment(state, strength)
        if state == "none":
            base = 0  # 需求①：无清晰主线 → 空仓观望
    # step3 事件档位：唯一判据在 event_adjustment（d==0 未落档的「封顶 1」已折算进
    # delta），本函数只负责统一 clamp，不再另判。
    base_after_mainline = base + ml_adj
    event_delta, event_reason = event_adjustment(
        event_d=event_d,
        event_result=event_result,
        mainline_state=state,
        mainline_strength=strength,
        gate_blocked=gate_blocked,
        base_index=base_after_mainline,
    )
    adj = ml_adj + event_delta
    final = max(LADDER_MIN, min(LADDER_MAX, base_after_mainline + event_delta))
    # 事件前档位（step2 结束、step3 之前）：盘前撤销「临近 +1」的唯一无歧义基线
    event_off_index = max(LADDER_MIN, min(LADDER_MAX, base_after_mainline))
    # 幂等闸门（职责重定义 spec §4.1）：事件调整「已计入」的类型（approaching / result）
    applied_kind = event_applied_kind(
        event_d=event_d,
        event_result=event_result,
        gate_blocked=gate_blocked,
        delta=event_delta,
        reason=event_reason,
    )
    ledger = _build_position_ledger(
        final=final,
        base=base,
        adj=adj,
        gate_hit=gate_hit,
        ml_breakdown=ml_breakdown,
        gate_blocked=gate_blocked,
        trend_known=trend_known,
        bull=bull,
        bars=bars,
        close=c,
        ma20=ma20,
        state=state,
        strength=strength,
        mainline=mainline,
        event_d=event_d,
        event_result=event_result,
        event_off_index=event_off_index,
        event_applied_kind=applied_kind,
        breadth=breadth,
    )
    return final, ledger


# 档位序号 → (action, direction)：**仅审计/展示用，不参与任何判定**。
# 档位的唯一真相源是 `derive_position_index` 返回的序号；本表只把已定好的序号翻译成
# PositionBand 的字面量字段，禁止任何逻辑按 action/direction 反推档位（否则就是第三套规则）。
# 原 `compute_position` 中「certainty=low 就降为 hold」的规则随该函数一并取消：
# certainty 只描述证据强度，不改档位（双真相源的一部分）。
_POSITION_ACTION_BY_INDEX: dict[int, tuple[str, str]] = {
    0: ("hold", "neutral"),   # 空仓观望
    1: ("add", "bullish"),    # 轻仓试探
    2: ("hold", "bullish"),   # 半仓持有
    3: ("add", "bullish"),    # 重仓顺势
    4: ("reduce", "bearish"),  # 满仓该减
}


def position_action(idx: int) -> tuple[PositionAction, Direction]:
    """档位序号 → (action, direction)（纯翻译，不参与判定，见上表注释）。"""
    action, direction = _POSITION_ACTION_BY_INDEX[idx]
    return cast("PositionAction", action), cast("Direction", direction)


def _build_position_ledger(
    *,
    final: int,
    base: int,
    adj: int,
    gate_hit: bool,
    ml_breakdown: bool,
    gate_blocked: bool,
    trend_known: bool,
    bull: bool,
    bars: int,
    close: float | None,
    ma20: float | None,
    state: object,
    strength: object,
    mainline: dict[str, object] | None,
    event_d: int | None,
    event_result: str | None,
    event_off_index: int,
    event_applied_kind: EventAppliedKind | None,
    breadth: dict[str, Any] | None,
) -> PositionLedger:
    """把 step0-4 的中间量组织成账本（纯展示层，不参与任何判定）。

    只做「已算出的量 → 文本」的翻译；任何判定分支必须留在 `derive_position_index`，
    防止账本反向成为第二真相源。
    """
    # 闸门禁止 +1 的具体原因（用于 miss/supporting 说明）
    if gate_hit:
        block_reason = "关键条件不满足"
    elif ml_breakdown:
        block_reason = "主线破位"
    elif not trend_known:
        block_reason = "趋势数据不足"
    else:
        block_reason = ""
    trend_base = 3 if bull else 2

    # —— trend ——
    if close is not None and ma20 is not None:
        trend_value = f"收盘 {close:.2f} 点，20 日均线 {ma20:.2f} 点"
    else:
        trend_value = f"只有 {bars} 根 K 线，至少要 20 根"
    if gate_hit:
        trend_item = LedgerItem(
            key="trend",
            value_text=trend_value,
            judgement="关键条件不满足",
            effect="一票否决：直接压到空仓",
            status="blocking",
        )
    elif not trend_known:
        trend_item = LedgerItem(
            key="trend",
            value_text=trend_value,
            judgement="数据不可用",
            effect="趋势判不了，先按两成算，但不能加",
            status="missing",
        )
    elif ml_breakdown:
        trend_item = LedgerItem(
            key="trend",
            value_text=trend_value,
            judgement="多头排列" if bull else "震荡",
            effect=f"按 {trend_base} 成打底，但主线破位压到空仓",
            status="neutral",
        )
    else:
        trend_item = LedgerItem(
            key="trend",
            value_text=trend_value,
            judgement="多头排列" if bull else "震荡",
            effect=f"按 {trend_base} 成打底",
            status="supporting" if bull else "neutral",
        )

    # —— mainline ——
    ml_name = (mainline or {}).get("name")
    ml_excess = (mainline or {}).get("excess")
    if ml_breakdown:
        mainline_item = LedgerItem(
            key="mainline",
            value_text=f"{ml_name} 破位" if ml_name else "破位标记",
            judgement="主线破位",
            effect="一票否决：直接压到空仓",
            status="blocking",
        )
    elif state == "none":
        excess_text = f"（最高超额 +{ml_excess:.1f} 个百分点）" if ml_excess is not None else ""
        mainline_item = LedgerItem(
            key="mainline",
            value_text=f"无清晰主线{excess_text}",
            judgement="没有出现",
            effect="直接压到空仓",
            status="blocking",
        )
    elif state == "established" and strength == "strong":
        if gate_blocked:
            mainline_item = LedgerItem(
                key="mainline",
                value_text=f"{ml_name or '主线'}（成立·强）",
                judgement="成立·强",
                effect=f"暂不加仓（{block_reason}）",
                status="neutral",
            )
        elif event_d == 0 and event_result in EVENT_RESULT_ENUM:
            mainline_item = LedgerItem(
                key="mainline",
                value_text=f"{ml_name or '主线'}（成立·强）",
                judgement="成立·强",
                effect="本可加仓，但事件落档已覆盖",
                status="neutral",
            )
        else:
            mainline_item = LedgerItem(
                key="mainline",
                value_text=f"{ml_name or '主线'}（成立·强）",
                judgement="成立·强",
                effect="可以加仓",
                status="supporting",
            )
    elif state == "established":
        mainline_item = LedgerItem(
            key="mainline",
            value_text=f"{ml_name or '主线'}（成立·弱）",
            judgement="成立·弱",
            effect="不影响仓位",
            status="neutral",
        )
    else:
        # None / "unavailable"：数据不可用必须显式标注，不得伪装中性
        # （未知态已在入口归一为 "unavailable"，此处不再透出原始值）
        mainline_item = LedgerItem(
            key="mainline",
            value_text="无主线数据" if state is None else "板块数据不足",
            judgement="数据不可用",
            effect="不影响仓位",
            status="missing",
        )

    # —— event ——
    event_item = _build_event_ledger_item(
        event_d=event_d,
        event_result=event_result,
        gate_blocked=gate_blocked,
        block_reason=block_reason,
        state=state,
        strength=strength,
    )

    # —— breadth ——
    breadth_item = _build_breadth_ledger_item(breadth)

    return PositionLedger(
        items=[trend_item, mainline_item, event_item, breadth_item],
        base_index=base,
        adjust=adj,
        final_index=final,
        gate_hit=gate_hit,
        mainline_breakdown=None if mainline is None else ml_breakdown,
        insufficient=not trend_known,
        gate_blocked=gate_blocked,
        mainline_state=state if isinstance(state, str) else None,
        event_off_index=event_off_index,
        event_applied_kind=event_applied_kind,
        watch_points=[],
        ledger_version="1.0",
    )


def _breadth_number(value: object) -> float | None:
    """数值化（bool 不是比例，按缺失处理）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _breadth_ratio(breadth: dict[str, Any] | None) -> float | None:
    """上涨占比（0-1）：优先 advance_ratio，缺失时按 advance_count/total_count 算。"""
    if not breadth:
        return None
    ratio = _breadth_number(breadth.get("advance_ratio"))
    if ratio is not None and 0.0 <= ratio <= 1.0:
        return ratio
    total = _breadth_number(breadth.get("total_count"))
    advance = _breadth_number(breadth.get("advance_count"))
    if total is not None and advance is not None and total > 0:
        value = advance / total
        if 0.0 <= value <= 1.0:
            return value
    return None


def _build_breadth_ledger_item(breadth: dict[str, Any] | None) -> LedgerItem:
    """宽度项账本：只展示，不参与档位判定（宽度仅参与 certainty）。"""
    ratio = _breadth_ratio(breadth)
    if ratio is None:
        return LedgerItem(
            key="breadth",
            value_text="上涨占比不可用",
            judgement="数据不可用",
            effect="只影响把握程度，不改仓位",
            status="missing",
        )
    if ratio >= 0.6:
        judgement = "偏强"
    elif ratio <= 0.35:
        judgement = "偏弱"
    else:
        judgement = "中性"
    return LedgerItem(
        key="breadth",
        value_text=f"上涨占比 {ratio * 100:.0f}%",
        judgement=judgement,
        effect="只影响把握程度，不改仓位",
        status="neutral",
    )


def _build_event_ledger_item(
    *,
    event_d: int | None,
    event_result: str | None,
    gate_blocked: bool,
    block_reason: str,
    state: object,
    strength: object,
) -> LedgerItem:
    """事件项账本：只翻译 step3 已产生的效果，不做判定。"""
    if event_d is None:
        return LedgerItem(
            key="event",
            value_text="无事件 / d 不可用",
            judgement="数据缺失",
            effect="不影响仓位",
            status="missing",
        )
    if event_d == 0 and event_result in EVENT_RESULT_ENUM:
        value_text = f"事件当天，结果{event_result}"
    elif event_d == 0:
        value_text = "事件当天，结果还没出来"
    elif event_d in (1, 2):
        value_text = f"距事件还有 {event_d} 个交易日，在观察窗口内"
    else:
        value_text = f"距事件还有 {event_d} 个交易日"

    if gate_blocked:
        return LedgerItem(
            key="event", value_text=value_text, judgement="没有变化",
            effect=f"暂不加仓（{block_reason}）", status="neutral",
        )
    if event_d in (1, 2):
        if state == "established" and strength == "strong":
            return LedgerItem(
                key="event", value_text=value_text, judgement="事件支持加仓",
                effect="可以加仓", status="supporting",
            )
        return LedgerItem(
            key="event", value_text=value_text, judgement="没有变化",
            effect="暂不加仓（主线还不够强）", status="neutral",
        )
    if event_d == 0 and event_result in EVENT_RESULT_ENUM:
        direction = EVENT_RESULT_DIRECTION[event_result]
        if direction == "bullish":
            return LedgerItem(
                key="event", value_text=value_text, judgement="事件支持加仓",
                effect=f"加仓（{event_result}）", status="supporting",
            )
        if direction == "bearish":
            return LedgerItem(
                key="event", value_text=value_text, judgement="事件要求减仓",
                effect=f"减仓（{event_result}）", status="blocking",
            )
        return LedgerItem(
            key="event", value_text=value_text, judgement="没有变化",
            effect=f"不调整（{event_result}）", status="neutral",
        )
    if event_d == 0:
        return LedgerItem(
            key="event", value_text=value_text, judgement="仓位封顶轻仓",
            effect="仓位最多一成，不能再加", status="blocking",
        )
    return LedgerItem(
        key="event", value_text=value_text, judgement="没有变化",
        effect="不影响仓位", status="neutral",
    )


def derive_position_text(
    *,
    index_closes: list[float],
    mainline: dict[str, object] | None,
    event_d: int | None,
    event_result: str | None,
) -> str:
    """仓位阶梯文案（薄包装，判定全部在 `derive_position_index`）。

    保留纯字符串入口供既有调用方/测试使用；文案与序号必同源（同一次 idx 取
    `POSITION_LADDER[idx]`），禁止另行重算。
    """
    idx, _ = derive_position_index(
        index_closes=index_closes, mainline=mainline,
        event_d=event_d, event_result=event_result,
    )
    return "建议仓位：" + POSITION_LADDER[idx]


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
    highs: list[float | None],
    lows: list[float | None],
    amounts: list[float],
    dense_support: float | None = None,
    dense_pressure: float | None = None,
    data_missing: list[str] | None = None,
    from_index: int | None = None,
    gate_blocked: bool = False,
) -> list[dict[str, Any]]:
    """确定性技术点位节点（§19.2，G19：点位由 engine 按当日行情计算）。

    条件分档（§19.5 interval 形态，互斥）：
    - 主用成交额 vs 20 日均量三档（放量/缩量/平量）；
    - amount 缺失 → 退化为指数点位三档（站上压力/跌破支撑/区间内）。
    - high/low 空值兜底（2026-09-05 裁决）：剔除空值行，整体仍不可得则
      返回空 branches 并在 data_missing 留痕，绝不伪造支撑/压力点位。

    `from_index` / `gate_blocked` 透传给全部 `position_band_to_action` 调用点
    （因果输出 spec §7）：分支的仓位动作必须接当前档位，闸门命中时 bullish
    分支被压制，不得承诺系统不会执行的加仓。

    highs/lows 形参声明为 list[float | None]（真实契约）：K 线原始行的 high/low
    可能缺失，本函数内部按行过滤 None（下方 recent_highs/recent_lows），故可安全承载。
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
                "position_action": position_band_to_action(
                    POSITION_BANDS["active"], "bullish",
                    from_index=from_index, gate_blocked=gate_blocked,
                ),
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
                "position_action": position_band_to_action(
                    POSITION_BANDS["ice"], "bearish",
                    from_index=from_index, gate_blocked=gate_blocked,
                ),
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
                "position_action": position_band_to_action(
                    POSITION_BANDS["normal"], "neutral",
                    from_index=from_index, gate_blocked=gate_blocked,
                ),
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
            "position_action": position_band_to_action(
                POSITION_BANDS["active"], "bullish",
                from_index=from_index, gate_blocked=gate_blocked,
            ),
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
            "position_action": position_band_to_action(
                POSITION_BANDS["ice"], "bearish",
                from_index=from_index, gate_blocked=gate_blocked,
            ),
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
            "position_action": position_band_to_action(
                POSITION_BANDS["normal"], "neutral",
                from_index=from_index, gate_blocked=gate_blocked,
            ),
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
# DEPRECATED 并已移除（2026-11-04）：远端 be1c558 曾以「仓位文案 → 阶梯序」的映射
# 实现 d=0 未落档封顶（cap_position_text_on_event_no_result）。该做法是反向推断，
# 与本次重构「序号是唯一真相源、文案只能由序号派生」的原则冲突，且重构后该函数零调用点。
# 等价语义已改在档位层面实现：rhythm_master._compose_card 的「事件当期出口闸」
# （position_index 封顶 1，不受 ±1 约束），并有单测覆盖。


def build_event_branch(
    event: dict[str, Any],
    origin_date: str | None = None,
    *,
    from_index: int | None = None,
    gate_blocked: bool = False,
) -> list[dict[str, Any]]:
    """事件节点（spec §19.2/D10/D15 + 本 spec §5.4/G12）：枚举分档（预期差）。

    只对 high 级事件生成 3 条互斥情景；公布后由确定性 d 逻辑按预期差落档。

    origin_date 提供时启用 d 约束：交易日差 d > EVENT_BRANCH_MAX_D 的事件不产分支
    （避免「闸门说无影响 vs 分支说超预期加仓」的同卡矛盾，spec §5.5.2）。
    返回 [] 表示非 high 事件（无事件分支）或 d 超限（同语义不产分支）。

    `from_index` / `gate_blocked` 透传给 `position_band_to_action`（因果输出 spec §7）：
    「超预期 → 加仓」同样要接当前档位，闸门命中时不得承诺加仓。

    公布后"点亮/置灰"能力当前未接线（spec D3 拆契约），故分支不含 met 键；
    event_ref 保留供验证器使用。
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
    note_suffix = "（该情景为条件态，不改仓位）"
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
                "position_action": position_band_to_action(
                    POSITION_BANDS[band_key], direction,
                    from_index=from_index, gate_blocked=gate_blocked,
                ),
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
    即当天、收盘定档为次一交易日），使「距今天数」相对卡片描述的那一天。
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

# importance 档序（展示过滤用）：high > medium > low。
_IMPORTANCE_ORDER: dict[str, int] = {"high": 2, "medium": 1, "low": 0}


def _importance_ge(importance: str, minimum: str) -> bool:
    return _IMPORTANCE_ORDER.get(importance, 0) >= _IMPORTANCE_ORDER.get(minimum, 1)


def project_event_window(
    events: list[dict[str, object]],
    *,
    importance_min: str | None = None,
    limit: int | None = None,
) -> list[dict[str, object]]:
    """卡片事件日历投影（spec §5.2 + 2026-09-21 需求 2）：`{date,type,title,importance}` 四键。

    前端 `RhythmEvent` 契约只认这四键（`RhythmCard.vue` 事件日历块）；`type` 缺失
    或非法 → 落 `seed`（前端 eventTypeLabel 有该映射），不返回 null。

    `importance_min`（可选）：仅保留 importance >= 该档的事件（high>medium>low），
    展示窗放全量后用于丢弃 low 噪音（G1）；缺省 None = 不过滤（既有契约不变）。
    `limit`（可选）：投影结果条数上限（防 30 日/全量窗列表爆炸）；缺省 None = 不限。
    """
    out: list[dict[str, object]] = []
    for e in events:
        title = str(e.get("title") or "")
        if not title:
            continue
        importance = str(e.get("importance") or "medium")
        if importance_min is not None and not _importance_ge(importance, importance_min):
            continue
        etype = str(e.get("type") or "")
        if etype not in _EVENT_TYPES:
            etype = _EVENT_TYPE_FALLBACK
        out.append({
            "date": str(e.get("date") or ""),
            "type": etype,
            "title": title,
            "importance": importance,
        })
        if limit is not None and len(out) >= limit:
            break
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
