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
