"""② 趋势反转确定性判定（spec §5.4.2 / 需求②）。

语义：放量阴线（前置事件）∧ 其后摆动点结构抬高（确认条件）→ confirmed。
数据不足一律 fail-safe：insufficient=True 且 confirmed=False（H5）。
"""
from __future__ import annotations

from aistock_agent.services.rhythm_engine import (
    MIN_BARS_FOR_REVERSAL,
    SWING_CONFIRM_BARS,
    SWING_LEFT_RIGHT,
    SWING_MIN_DELTA,
    VOL_UP_RATIO,
)


def _avg(values: list[float], n: int) -> float:
    return sum(values[-n:]) / n


def _swing_series(values: list[float]) -> list[tuple[int, float]]:
    """返回局部极值（左右各 SWING_LEFT_RIGHT 根）索引与值。"""
    out: list[tuple[int, float]] = []
    for i in range(SWING_LEFT_RIGHT, len(values) - SWING_LEFT_RIGHT):
        window = values[i - SWING_LEFT_RIGHT: i + SWING_LEFT_RIGHT + 1]
        if values[i] == min(window):
            out.append((i, values[i]))
        if values[i] == max(window):
            out.append((i, values[i]))
    return out


def _rising(points: list[float]) -> bool:
    """最近两个摆动点是否依次抬高（增量 ≥ SWING_MIN_DELTA 相对比较，防噪声）。"""
    if len(points) < 2:
        return False
    return points[-1] > points[-2] * (1 + SWING_MIN_DELTA)


def detect_trend_reversal(
    closes: list[float],
    opens: list[float],
    highs: list[float],
    lows: list[float],
    amounts: list[float],
) -> dict[str, object]:
    """趋势反转确定性判定（spec §5.4.2 / 需求②）。

    前置事件 = 最近一根「放量阴线」（close < open 且 amount > 前 20 根均量 × VOL_UP_RATIO）；
    确认条件 = 观察起点之后已完成 bar ≥ SWING_CONFIRM_BARS，且最近两个 swing low / swing high
    各自依次抬高 → confirmed=True。open 缺失的行剔除不参与（H5 fail-safe）。
    """
    rows = [
        (c, o, h, low_v, a)
        for c, o, h, low_v, a in zip(closes, opens, highs, lows, amounts)
        if o is not None and c is not None
    ]
    if len(rows) < MIN_BARS_FOR_REVERSAL:
        return {"confirmed": False, "insufficient": True,
                "reason": "完成 bar 不足 22 根，反转确认不可用（fail-safe）"}
    cs = [r[0] for r in rows]
    os = [r[1] for r in rows]
    as_ = [r[4] for r in rows]
    n = len(rows)
    # 前置事件：取最近一根放量阴线为观察起点
    obs: int | None = None
    for i in range(n - 1, -1, -1):
        if cs[i] >= os[i]:
            continue
        if i <= 0:
            continue
        avg20 = _avg(as_[:i], min(i, 20))
        if avg20 > 0 and as_[i] > avg20 * VOL_UP_RATIO:
            obs = i
            break
    if obs is None:
        return {"confirmed": False, "insufficient": False, "reason": "无放量阴线前置事件"}
    tail = cs[obs:]
    # 观察起点之后的已完成 bar 数
    if len(tail) - 1 < SWING_CONFIRM_BARS:
        return {"confirmed": False, "insufficient": False,
                "reason": "放量阴线后完成 bar 不足，结构未确认"}
    swing_lows: list[float] = []
    swing_highs: list[float] = []
    for idx, v in _swing_series(tail):
        window = tail[max(0, idx - SWING_LEFT_RIGHT): idx + SWING_LEFT_RIGHT + 1]
        if v == min(window):
            swing_lows.append(v)
        if v == max(window):
            swing_highs.append(v)
    if _rising(swing_lows) and _rising(swing_highs):
        return {"confirmed": True, "insufficient": False,
                "reason": "放量阴线后低点/高点摆动结构抬高"}
    return {"confirmed": False, "insufficient": False,
            "reason": "放量阴线后摆动结构未抬高"}
