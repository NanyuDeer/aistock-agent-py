import pytest

from scripts.calibration.k_band import band_k, cumulative_returns


def test_cumulative_returns_is_compound_not_sum():
    # 两日各 +1%：复利 2.01% ≠ 简单求和 2.0%
    assert cumulative_returns([1.0, 1.0], window=2) == [pytest.approx(2.01, abs=1e-6)]


def test_cumulative_returns_slides_over_series():
    # 3 个点、窗口 2 → 2 个样本
    out = cumulative_returns([1.0, 2.0, 3.0], window=2)
    assert len(out) == 2
    assert out[0] == pytest.approx((1.01 * 1.02 - 1) * 100, abs=1e-6)
    assert out[1] == pytest.approx((1.02 * 1.03 - 1) * 100, abs=1e-6)


def test_band_k_is_one_third_quantile_of_abs():
    # 9 个样本，|x| 的 1/3 分位（线性插值，pos=(1/3)*(9-1)=8/3）
    # → 3*(1-2/3) + 4*(2/3) = 3.6667；此时 |x| < k 恰为 3/9 = 1/3。
    xs = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]
    k = band_k(xs, inside=1 / 3)
    assert k == pytest.approx(3.0 * (1 / 3) + 4.0 * (2 / 3), abs=1e-6)
    assert sum(1 for v in xs if v < k) / len(xs) == pytest.approx(1 / 3, abs=1e-9)


def test_band_k_raises_on_empty():
    with pytest.raises(ValueError):
        band_k([])
