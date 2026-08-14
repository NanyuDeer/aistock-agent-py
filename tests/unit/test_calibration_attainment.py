"""report_event_attainment 的达标统计逻辑（五期）。"""

import json
from pathlib import Path

from scripts.calibration.report_event_attainment import (
    _collect_event_cases,
    compute_event_attainment,
)


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


def test_collect_event_cases_skips_top_level_array_files(tmp_path: Path) -> None:
    """I-1：合法 JSON 但顶层非 dict（数组）的 iterated 标记 / best.json / GT 文件
    → 跳过不崩（与 docstring"损坏→跳过"一致）。"""
    exps = tmp_path / "experiments"
    exps.mkdir()
    cases = tmp_path / "cases"
    cases.mkdir()
    gt_dir = tmp_path / "ground_truths"
    gt_dir.mkdir()
    # iterated 标记为数组 → 该 case 跳过
    c1 = "case_20260814_event_analyst_dirty1"
    (exps / f"{c1}_best.json").write_text(
        json.dumps({"score": 0.9, "round": 2}), encoding="utf-8",
    )
    (cases / f"{c1}.iterated.json").write_text("[1, 2, 3]", encoding="utf-8")
    # best.json 为数组 → 该 case 跳过
    c2 = "case_20260814_event_analyst_dirty2"
    (cases / f"{c2}.iterated.json").write_text(
        json.dumps({"status": "iterated"}), encoding="utf-8",
    )
    (exps / f"{c2}_best.json").write_text("[1, 2, 3]", encoding="utf-8")
    # GT 文件为数组 → confidence=unknown（不崩，非 low 计入达标判定）
    c3 = "case_20260814_event_analyst_dirty3"
    (cases / f"{c3}.iterated.json").write_text(
        json.dumps({"status": "iterated"}), encoding="utf-8",
    )
    (exps / f"{c3}_best.json").write_text(
        json.dumps({"score": 0.8, "round": 1}), encoding="utf-8",
    )
    (gt_dir / f"gt_{c3}.json").write_text("[1, 2, 3]", encoding="utf-8")

    collected = _collect_event_cases(tmp_path)
    assert len(collected) == 1  # c1/c2 跳过，仅 c3 收录
    assert collected[0]["case_id"] == c3
    assert collected[0]["gt_confidence"] == "unknown"
