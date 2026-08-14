"""report_event_attainment 的达标统计逻辑（五期）。"""

from scripts.calibration.report_event_attainment import compute_event_attainment


def test_compute_event_attainment_rates() -> None:
    """达标率 = best_score >= target 且 GT confidence != low 占比；轮数分布正确。"""
    cases = [
        {"best_score": 0.85, "gt_confidence": "high", "best_round": 5},
        {"best_score": 0.85, "gt_confidence": "low", "best_round": 3},   # low GT 不算达标
        {"best_score": 0.6, "gt_confidence": "medium", "best_round": 8},
        {"best_score": 0.9, "gt_confidence": "medium", "best_round": 10},
    ]
    stats = compute_event_attainment(cases, target_score=0.8, max_rounds=10)
    assert stats["attainment_rate"] == 0.5           # 2/4（low 不计、0.6 不达标）
    assert stats["avg_rounds"] == 6.5
    assert stats["median_rounds"] == 6.5
    assert stats["max_rounds_exhausted"] == 1        # best_round == max_rounds 的 case 数


def test_compute_event_attainment_empty() -> None:
    """空输入 → 全零统计（不崩）。"""
    stats = compute_event_attainment([], target_score=0.8, max_rounds=10)
    assert stats["attainment_rate"] == 0.0
    assert stats["avg_rounds"] == 0.0
