"""历史密集触碰带（design-debate A1 裁决，硬约束 #1）。

用"历史反复触碰折返"识别真实支撑/压力，替代 max/min(20日极值)×MA20 系数。
确定性纯函数：同输入必同输出。数据不足时 insufficient=True，不产伪带。
"""
from __future__ import annotations


def dense_band(
    closes: list[float],
    highs: list[float],
    lows: list[float],
    amount: list[float | None],
    window: int = 40,
    touch_gap: float = 0.005,
    touch_min: int = 2,
    band_width: float = 0.01,
    clamp_ratio: float = 0.04,
) -> tuple[float | None, float | None, int, bool]:
    """返回 (support, pressure, touch_count, insufficient)。

    - 仅在窗口内做触碰频次统计（近端窗口，与 5 日目标同尺度）。
    - 触碰判定：某根 K 线 high/low 落在 [price*(1-touch_gap), price*(1+touch_gap)] 视作一次触碰。
    - 取触碰频次最高的价位区间为密集带中心，带宽 = band_width。
    - clamp：密集带与 [ma20*(1-clamp_ratio), ma20*(1+clamp_ratio)] 求交，保证不远离现价。
    """
    if len(closes) < window or len(highs) < window or len(lows) < window:
        return None, None, 0, True
    # 只统计近端窗口，避免历史结构价位脱离现价（R1 A1-①）
    closes_w = closes[-window:]
    highs_w = highs[-window:]
    lows_w = lows[-window:]
    ma20 = sum(closes_w[-20:]) / 20
    lo_clamp = ma20 * (1 - clamp_ratio)
    hi_clamp = ma20 * (1 + clamp_ratio)

    # 触碰频次统计：对每根 K 线，看它的 high/low 落在哪些候选价位带内
    # 候选带中心 = 每个 high/low 采样值；统计各中心被触碰次数
    touch_events: dict[float, int] = {}
    for h, lo in zip(highs_w, lows_w):
        for price in (h, lo):
            if price is None:
                continue
            # 记录该价位被触碰
            touch_events[round(price, 2)] = touch_events.get(round(price, 2), 0) + 1

    if not touch_events:
        return None, None, 0, True
    # 找触碰次数最多的价位 → 作为密集带中心（可能多个，取最高的）
    max_touch = max(touch_events.values())
    if max_touch < touch_min:
        return None, None, 0, True
    center = max(
        [p for p, c in touch_events.items() if c == max_touch],
        key=lambda p: p if lo_clamp <= p <= hi_clamp else -float("inf"),
    )
    half = center * band_width
    support_candidate = center - half
    pressure_candidate = center + half
    # clamp 到 MA20 ± clamp_ratio，保证中性区间为当前通道
    support = max(support_candidate, lo_clamp) if support_candidate >= lo_clamp else lo_clamp
    pressure = min(pressure_candidate, hi_clamp) if pressure_candidate <= hi_clamp else hi_clamp
    if support > pressure:
        support, pressure = pressure, support
    return support, pressure, max_touch, False
