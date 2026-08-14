"""export_calibration_set 的样本挑选与候选组装逻辑（五期）。"""

import json
from pathlib import Path

from scripts.calibration.export_calibration_set import _collect_samples, pick_calibration_samples


def test_pick_calibration_samples_balances_agents_and_directions() -> None:
    """10 条样本：review/event 各 ≥3、方向性（bullish/bearish）覆盖、judge 分数分层。"""
    samples = [
        {"case_id": f"c{i}", "agent_id": "review" if i % 2 == 0 else "event_analyst",
         "gt_attribution": {"direction": "bullish" if i % 3 == 0 else "bearish"},
         "judge_score": 0.3 + 0.05 * i}
        for i in range(30)
    ]
    picked = pick_calibration_samples(samples, target=10)
    assert len(picked) == 10
    agents = {p["agent_id"] for p in picked}
    assert "review" in agents and "event_analyst" in agents
    assert any(p["gt_attribution"]["direction"] == "bullish" for p in picked)
    assert any(p["gt_attribution"]["direction"] == "bearish" for p in picked)
    # judge 分数分层：被选样本的分数覆盖低/中/高
    picked_scores = [p["judge_score"] for p in picked]
    assert min(picked_scores) < 0.45 and max(picked_scores) > 0.65


def test_pick_calibration_samples_undersupply() -> None:
    """样本不足（< target）→ 返回全部可用（尽量覆盖）。"""
    samples = [{"case_id": f"c{i}", "agent_id": "review",
                "gt_attribution": {"direction": "bearish"}, "judge_score": 0.5} for i in range(4)]
    picked = pick_calibration_samples(samples, target=10)
    assert len(picked) == 4


def test_collect_samples_assembles_candidates_from_data_dirs(tmp_path: Path) -> None:
    """_collect_samples：只收已迭代 case，agent_id 按 case_id 实际形态提取，
    gt_attribution/judge_score/human 字段组装正确（best.json 无 ground_truth_ref，
    按 case_builder 前缀约定推导 gt_id）。"""
    exps = tmp_path / "experiments"
    exps.mkdir()
    (exps / "case_20260814_review_test1_best.json").write_text(
        json.dumps({"score": 0.7, "round": 1, "patch": {"target_symbol": "x"}}),
        encoding="utf-8",
    )
    # 未迭代 case 的 best 文件应被过滤（iterated 标记缺失）
    (exps / "case_20260814_event_analyst_test2_best.json").write_text(
        json.dumps({"score": 0.8}), encoding="utf-8",
    )
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / "case_20260814_review_test1.iterated.json").write_text(
        json.dumps({"status": "iterated"}), encoding="utf-8",
    )
    gt_dir = tmp_path / "ground_truths"
    gt_dir.mkdir()
    (gt_dir / "gt_case_20260814_review_test1.json").write_text(
        json.dumps(
            {
                "gt_id": "gt_case_20260814_review_test1",
                "attribution": {"direction": "bullish"},
            }
        ),
        encoding="utf-8",
    )

    samples = _collect_samples(tmp_path)
    assert len(samples) == 1
    s = samples[0]
    assert s["case_id"] == "case_20260814_review_test1"
    assert s["gt_id"] == "gt_case_20260814_review_test1"
    assert s["agent_id"] == "review"
    assert s["gt_attribution"] == {"direction": "bullish"}
    assert s["agent_best_attribution"] == {}
    assert s["judge_score"] == 0.7
    assert s["human"] == {
        "direction_score": None,
        "drivers_score": None,
        "sectors_score": None,
        "confidence": None,
    }


def test_collect_samples_resolves_agent_id_from_case_dir(tmp_path: Path) -> None:
    """agent_id 从 case 文件归档目录反查（event_analyst 含下划线不被截断），
    gt_id 优先取 case 文件 ground_truth_ref。"""
    case_id = "case_20260814_event_analyst_test3"
    exps = tmp_path / "experiments"
    exps.mkdir()
    (exps / f"{case_id}_best.json").write_text(
        json.dumps({"score": 0.66, "round": 3}), encoding="utf-8",
    )
    (tmp_path / "cases").mkdir()
    (tmp_path / "cases" / f"{case_id}.iterated.json").write_text(
        json.dumps({"status": "iterated"}), encoding="utf-8",
    )
    agent_dir = tmp_path / "cases" / "event_analyst"
    agent_dir.mkdir()
    (agent_dir / f"{case_id}.json").write_text(
        json.dumps({"case_id": case_id, "ground_truth_ref": f"gt_{case_id}"}),
        encoding="utf-8",
    )

    samples = _collect_samples(tmp_path)
    assert len(samples) == 1
    assert samples[0]["agent_id"] == "event_analyst"
    assert samples[0]["gt_id"] == f"gt_{case_id}"
