"""异常检测：把「今天没事」与「今天动了但形态不认识」分开。"""

from __future__ import annotations

from aistock_agent.services.anomaly_detection import assess_anomaly


def _facts(
    *,
    csi300: float = 0.0,
    csi1000: float = 0.0,
    limit_up: int = 10,
    limit_down: int = 10,
    turnover_change: float = 0.0,
) -> dict[str, object]:
    return {
        "indexes": {
            "SH000300": {"ts_code": "000300.SH", "change_pct": csi300},
            "SH000852": {"ts_code": "000852.SH", "change_pct": csi1000},
        },
        "breadth": {"advance_ratio": 0.5, "total_count": 5000, "decline_count": 2500},
        "limits": {"up_count": limit_up, "down_count": limit_down, "broken_count": 1},
        "turnover": {"change_pct": turnover_change},
    }


def test_quiet_day_is_no_anomaly() -> None:
    """各项指标都在阈值内 → 不需要归因。"""
    result = assess_anomaly(_facts(csi300=0.2, csi1000=0.3), matched_phenomenon=False)

    assert result.has_anomaly is False
    assert result.anomaly_kind == "none"
    assert result.gate == "no_anomaly"


def test_2026_11_05_style_spread_is_detected_and_attributable() -> None:
    """11-05：沪深300 +0.29 vs 国证2000 +2.40，差值 2.11pp → 有异常且形态已识别。"""
    result = assess_anomaly(
        _facts(csi300=0.29, csi1000=2.40),
        matched_phenomenon=True,  # 补了差值判据后，style_divergence 应命中
    )

    assert result.has_anomaly is True
    assert result.anomaly_kind == "sector_rotation"
    assert result.magnitude >= 1.0
    assert result.gate == "attributable"


def test_anomaly_without_matching_phenomenon_is_unclassified() -> None:
    """有异常但五个桶都不认识 → 分类体系有缺口，不能当成「今天没事」。"""
    result = assess_anomaly(
        _facts(csi300=0.29, csi1000=2.40),
        matched_phenomenon=False,
    )

    assert result.has_anomaly is True
    assert result.gate == "unclassified_anomaly"


def test_limit_extreme_is_breadth_anomaly() -> None:
    """涨跌停家数极端 → breadth 异常。"""
    result = assess_anomaly(
        _facts(limit_up=80, limit_down=5),
        matched_phenomenon=True,
    )

    assert result.has_anomaly is True
    assert result.anomaly_kind == "breadth"


def test_volume_spike_is_detected() -> None:
    result = assess_anomaly(
        _facts(turnover_change=45.0),
        matched_phenomenon=False,
    )

    assert result.has_anomaly is True
    assert result.anomaly_kind == "volume"


def test_empty_facts_do_not_raise_and_treat_as_quiet() -> None:
    """数据缺失时不得把「零值」误读成「极度缩量/零涨跌停 = 异常」。"""
    result = assess_anomaly({}, matched_phenomenon=False)

    assert result.has_anomaly is False
    assert result.gate == "no_anomaly"
