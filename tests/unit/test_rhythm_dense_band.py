from aistock_agent.services.rhythm_dense_band import dense_band


def test_insufficient_data():
    assert dense_band([1.0], [1.0], [1.0], [1.0], window=40) == (None, None, 0, True)


def test_deterministic_same_input_same_output():
    closes = [3000 + i * 2 for i in range(60)]
    highs = [c + 10 for c in closes]
    lows = [c - 10 for c in closes]
    amount = [100.0] * 60
    r1 = dense_band(closes, highs, lows, amount, window=40)
    r2 = dense_band(closes, highs, lows, amount, window=40)
    assert r1 == r2


def test_returns_band_within_price_range():
    closes = [3000 + i * 2 for i in range(60)]
    highs = [c + 10 for c in closes]
    lows = [c - 10 for c in closes]
    amount = [100.0] * 60
    support, pressure, touch_count, insufficient = dense_band(
        closes, highs, lows, amount, window=40
    )
    assert insufficient is False
    assert support is not None and pressure is not None
    assert support <= pressure
    assert touch_count >= 0


def test_repeated_touch_zone_yields_high_touch_count():
    # 构造一个价位区（约 3020）被反复触及：多次 high/low 落在 3020 附近
    closes = []
    highs = []
    lows = []
    for i in range(60):
        # 一半日子在 3020 附近震荡触碰，一半日子上行
        if i % 2 == 0:
            closes.append(3015 + (i % 5))
            highs.append(3030)
            lows.append(3010)
        else:
            closes.append(3050 + i)
            highs.append(3060 + i)
            lows.append(3040 + i)
    amount = [100.0] * 60
    support, pressure, touch_count, insufficient = dense_band(
        closes, highs, lows, amount, window=40
    )
    assert insufficient is False
    assert touch_count >= 2


def test_touch_gap_merges_close_prices():
    # 3019.98 与 3020.04 价差 0.06，touch_gap=0.5% 下应合并为同一档，
    # 而非被 round(price, 2) 拆成两个不同中心。
    closes = [3020.01] * 60
    highs = [3020.04] * 60
    lows = [3019.98] * 60
    amount = [100.0] * 60
    support, pressure, touch_count, insufficient = dense_band(
        closes, highs, lows, amount, window=40
    )
    assert insufficient is False
    # 40 根 K 线的 high(3020.04) + low(3019.98) 合并后共 80 次触碰，
    # 而非被拆成 40+40 两个档位（旧 round(price,2) 会返回 40）
    assert touch_count == 80
    # 同档验证：两个价位都被同一支撑/压力区间包含
    assert support <= 3019.98 < 3020.04 <= pressure


def test_amount_weighting_changes_winner():
    # 低量高频价位（约 3000，40 次触碰，amount=1）
    # vs 高量低频价位（约 3100，6 次触碰，amount=1000）
    # 量能加权时应由 3100 区胜出，等权退化时由 3000 区胜出。
    closes = [3000.0] * 40 + [3100.0] * 3 + [3050.0] * 17
    highs = [3002.0] * 40 + [3102.0] * 3 + [3052.0] * 17
    lows = [2998.0] * 40 + [3098.0] * 3 + [3048.0] * 17
    amount = [1.0] * 40 + [1000.0] * 3 + [100.0] * 17

    support, pressure, touch_count, insufficient = dense_band(
        closes, highs, lows, amount, window=40
    )
    assert insufficient is False
    # 高量低频区（3100）胜出
    assert touch_count == 6
    assert support <= 3100 <= pressure

    # amount 全 None -> 退化为等权（次数），低量高频区（3000）胜出
    support2, pressure2, touch_count2, insufficient2 = dense_band(
        closes, highs, lows, [None] * 60, window=40
    )
    assert insufficient2 is False
    assert touch_count2 == 40
    assert support2 <= 3000 <= pressure2
