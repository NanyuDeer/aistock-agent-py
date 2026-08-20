"""calibration —— 校准集方向判定偏差防护（A-1）"""

from aistock_agent.iterate.calibration import (
    CALIBRATION_SET,
    calibration_passed,
    run_calibration_static,
)


def test_calibration_set_has_ten_items() -> None:
    """校准集必须 10 条（裁决书 A 论题规格）。"""
    assert len(CALIBRATION_SET) == 10


def test_calibration_rules_cover_all_directions() -> None:
    """校准集三种方向标签齐全。"""
    directions = {item["direction"] for item in CALIBRATION_SET}
    assert directions == {"bullish", "bearish", "neutral"}


def test_run_calibration_static_high_hit_rate() -> None:
    """规则化判定命中率达标（>= 0.8）——关键词表漂移会在此暴露。"""
    assert run_calibration_static() >= 0.8


def test_calibration_passed_disabled_by_default(monkeypatch: object) -> None:
    """默认关闭：不校验直接放行（兼容存量部署）。"""
    from aistock_agent.iterate import calibration as cal

    monkeypatch.setattr(cal.settings, "iterate_calibration_required", False)  # type: ignore[attr-defined]
    assert calibration_passed() is True


def test_calibration_passed_gate_when_enabled(monkeypatch: object) -> None:
    """开启时命中率达标才放行；用坏关键词表模拟漂移拒绝。"""
    from aistock_agent.iterate import calibration as cal

    monkeypatch.setattr(cal.settings, "iterate_calibration_required", True)  # type: ignore[attr-defined]
    assert calibration_passed() is True  # 当前 10 条全部命中

    # 模拟关键词表漂移：bullish 关键词清空 → 命中率暴跌 → 拒绝上线
    monkeypatch.setattr(cal, "_BULLISH_KEYWORDS", ())  # type: ignore[attr-defined]
    assert calibration_passed() is False
