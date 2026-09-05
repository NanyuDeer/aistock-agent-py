"""锁定 _build_rhythm_card 的 high/low 缺失真实路径守卫（Fix 1）。

回归：Task 2 的 build_technical_branches 守卫会过滤 None 并在无有效值时返回空
branches + data_missing 留痕；本测试锁定"缺失行直传 None（而非 0.0）"不会被
引擎误当有效点位，从而不伪造支撑/压力。
"""
import pytest

from aistock_agent.agents.workers.rhythm_master import _build_rhythm_card
from aistock_agent.schemas.rhythm_master import MasterRhythmCard, RhythmEvidence


def _win():
    return type("W", (), {"events": [], "high_events": [], "source_missing": False})()


def _card():
    evidence = RhythmEvidence(stage="rally")
    return MasterRhythmCard(
        basis_date="2026-09-04", target_date="2026-09-05",
        refresh_slot="after_close", evidence=evidence,
    )


def _rows(n: int, *, high, low):
    return [
        {"close": 3000.0, "amount": 100.0, "high": high, "low": low}
        for _ in range(n)
    ]


def test_build_rhythm_card_missing_high_low_returns_empty_and_trace():
    """high/low 全缺失：branches 为空，data_missing 含技术支撑/压力留痕。"""
    card = _build_rhythm_card(_card(), _win(), _rows(60, high=None, low=None))
    assert card["branches"] == []
    assert any("技术支撑/压力" in m for m in card["data_missing"])


def test_build_rhythm_card_normal_high_low_returns_branches():
    """high/low 有效：branches 非空（引擎正常生成技术点位）。"""
    card = _build_rhythm_card(_card(), _win(), _rows(60, high=3010.0, low=2990.0))
    assert isinstance(card["branches"], list)
    assert card["branches"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
