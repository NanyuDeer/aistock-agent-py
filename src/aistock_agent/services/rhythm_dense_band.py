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
    - 触碰判定：先收集窗口内所有 K 线 high/low 作为候选价，按价格升序排序，
      再用贪心一维聚类把"相邻且价差 <= touch_gap*price"的价位并成一档。每档的
      触碰次数 = 落入该档的 high/low 点数。因此两价如 3019.98 与 3020.04
      （价差 0.06，touch_gap=0.5%）会合并到同一档，而非被 round(price, 2)
      拆成不同中心。
    - 量能加权：每档"触碰质量 mass" = 该档内每根 K 线成交额 amount 之和；当
      amount 全为 None/空时退化为等权（mass = 触碰次数）。用 mass 排序挑选密集带，
      量能大的档优先，量能仅用在此挑选（不改变公开签名/返回元组形状）。
    - 取触碰质量（或等权次数）最高的价位区间为密集带中心，带宽 = band_width。
    - clamp：密集带与 [ma20*(1-clamp_ratio), ma20*(1+clamp_ratio)] 求交，
      保证不远离现价。
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

    # 成交额加权：仅当 amount 非空且存在非 None 值时才启用，否则等权
    use_amount = amount is not None and len(amount) > 0 and any(v is not None for v in amount)
    amount_w = amount[-window:] if amount else []

    def candle_weight(j: int) -> float:
        if not use_amount:
            return 1.0
        if j < len(amount_w) and amount_w[j] is not None:
            return float(amount_w[j])
        return 0.0

    # 收集候选触碰价，带上该根 K 线的权重
    cand: list[tuple[float, float]] = []
    for j in range(window):
        w = candle_weight(j)
        h = highs_w[j]
        lo = lows_w[j]
        if h is not None:
            cand.append((float(h), w))
        if lo is not None:
            cand.append((float(lo), w))

    if not cand:
        return None, None, 0, True

    # 贪心一维聚类：按价升序，相邻价差 <= touch_gap*price 归同一档
    cand.sort(key=lambda x: x[0])
    clusters: list[tuple[float, int, float]] = []  # (center, count, mass)
    cur_items: list[tuple[float, float]] = []
    prev_price: float | None = None

    def flush() -> None:
        if not cur_items:
            return
        total_w = sum(w for _, w in cur_items)
        count = len(cur_items)
        # 中心用量能加权均值（成交额小/缺失时退化为算术均值）
        if total_w > 0:
            center = sum(p * w for p, w in cur_items) / total_w
        else:
            center = sum(p for p, _ in cur_items) / count
        clusters.append((center, count, total_w))

    for price, w in cand:
        if prev_price is not None and (price - prev_price) > touch_gap * price:
            flush()
            cur_items = []
        cur_items.append((price, w))
        prev_price = price
    flush()

    if not clusters:
        return None, None, 0, True

    # 按质量挑选密集带：质量（等权时=次数）为主，次数为辅，
    # 优先落在 clamp 区间内、贴近 ma20、价位更高者
    def sort_key(c: tuple[float, int, float]) -> tuple[float, int, int, float, float]:
        center, count, mass = c
        return (
            -mass,
            -count,
            0 if lo_clamp <= center <= hi_clamp else 1,
            abs(center - ma20),
            -center,
        )

    center, count, mass = min(clusters, key=sort_key)
    if count < touch_min:
        return None, None, 0, True

    half = center * band_width
    support_candidate = center - half
    pressure_candidate = center + half
    # clamp 到 MA20 ± clamp_ratio，保证中性区间为当前通道
    support = max(support_candidate, lo_clamp)
    pressure = min(pressure_candidate, hi_clamp)
    if support > pressure:
        support, pressure = pressure, support
    return support, pressure, count, False
