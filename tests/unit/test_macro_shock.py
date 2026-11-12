"""宏观冲击值计算单测。"""

from __future__ import annotations

from aistock_agent.services.macro_shock import (
    DEFAULT_THRESHOLDS,
    compute_macro_shock,
    compute_macro_shocks,
)


def test_direction_follows_gap_sign() -> None:
    # CPI 预期 0.8、实际 0.7 → gap=-0.1 → 低于预期
    assert compute_macro_shock("CN_CPI_YOY", -0.1) is not None
    shock = compute_macro_shock("CN_CPI_YOY", -0.1)
    assert shock is not None
    assert shock.direction == -1
    # |−0.1| / 0.2 = 0.5
    assert shock.strength == 0.5


def test_positive_gap_is_bullish_signal() -> None:
    shock = compute_macro_shock("CN_PPI_YOY", 0.4)
    assert shock is not None
    assert shock.direction == 1
    assert shock.strength == 1.0  # 0.4/0.3 超阈值，封顶 1


def test_zero_gap_has_direction_zero_but_still_reported() -> None:
    """符合预期也要返回——「没偏离」本身是信息，不等于「没数据」。"""
    shock = compute_macro_shock("CN_M2_YOY", 0.0)
    assert shock is not None
    assert shock.direction == 0
    assert shock.strength == 0.0


def test_missing_gap_returns_none() -> None:
    """没有预期值就无法判断超预期，不能当 0 处理。"""
    assert compute_macro_shock("CN_CPI_YOY", None) is None


def test_unconfigured_indicator_returns_none() -> None:
    """未配置阈值时不给默认值：错误量纲的阈值会造出假冲击。"""
    assert compute_macro_shock("CN_SOMETHING_NEW", 1.0) is None


def test_custom_thresholds_override() -> None:
    shock = compute_macro_shock("CN_CPI_YOY", 0.1, thresholds={"CN_CPI_YOY": 0.1})
    assert shock is not None
    assert shock.strength == 1.0


def test_all_default_thresholds_positive() -> None:
    for indicator, threshold in DEFAULT_THRESHOLDS.items():
        assert threshold > 0, f"{indicator} 阈值必须为正"


def test_batch_skips_unusable_entries() -> None:
    expectations: dict[str, dict[str, object]] = {
        "CN_CPI_YOY": {"gap": -0.1, "forecast": 0.8, "actual": 0.7},
        "CN_TSF": {"gap": None, "forecast": None},
        "US_PPI_YOY": {"gap": 0.3},
        "CN_UNKNOWN": {"gap": 5.0},
    }

    shocks = compute_macro_shocks(expectations)

    assert set(shocks) == {"CN_CPI_YOY", "US_PPI_YOY"}
    assert shocks["CN_CPI_YOY"].direction == -1
    assert shocks["US_PPI_YOY"].direction == 1


def test_batch_handles_empty_input() -> None:
    assert compute_macro_shocks({}) == {}
