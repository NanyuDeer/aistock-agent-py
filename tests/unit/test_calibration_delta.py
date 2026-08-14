"""compute_delta 的 δ=2σ 校准统计逻辑（五期）。"""

import json
from unittest.mock import patch

from scripts.calibration.compute_delta import compute_delta_from_scores, iter_experiment_scores


def _fake_path(name: str, content: str) -> object:
    """构造测试用伪 Path：仅暴露 .name / .read_text（兼容实现传 encoding 关键字）。"""

    return type("P", (), {"name": name, "read_text": lambda s, **kw: content})()


def _fake_data_dir(paths: list[object]) -> object:
    """构造具有 glob 方法的伪数据目录（委托给 patch 后的 Path.glob）。"""

    def glob(self: object, pattern: str) -> list[object]:
        return paths

    return type("D", (), {"glob": glob})()


def test_iter_experiment_scores_parses_round_files() -> None:
    """从 experiments 目录收集各 case 轮级 score（按轮号时序）；best 固化快照与
    非轮文件排除（I-3：best 不参与 δ 统计）。"""
    files = {
        "case_a_r1_baseline.json": json.dumps({"score": 0.4}),
        "case_a_r2.json": json.dumps({"score": 0.6}),
        "case_a_best.json": json.dumps({"score": 0.7}),
        "case_b_r1_baseline.json": json.dumps({"score": 0.5}),
        # reporter 写的 {date}_experiments.json 附件：非轮文件，不得被当作 case
        "2026-08-14_experiments.json": json.dumps({"total": 3}),
    }
    paths = [_fake_path(n, c) for n, c in files.items()]

    with patch("scripts.calibration.compute_delta.Path.glob") as mock_glob:
        mock_glob.return_value = paths
        scores = iter_experiment_scores(_fake_data_dir(paths))
    # best.json 不参与 δ 统计（轮文件已含 best 轮记录，重复计入产生伪零 Δ）
    assert scores == {"case_a": [0.4, 0.6], "case_b": [0.5]}


def test_iter_experiment_scores_skips_corrupted_files() -> None:
    """损坏文件（非法 JSON / 非数值 score / 非 dict）跳过，不中断收集。"""
    files = {
        "case_a_r1_baseline.json": '{"score": 0.4}',
        "case_a_r2.json": "{broken json",
        "case_a_best.json": '{"score": "nan_score"}',
        "case_b_r1_baseline.json": '{"score": 0.5}',
        "case_b_r2.json": "[1, 2, 3]",  # 合法 JSON 但非 dict
    }
    paths = [_fake_path(n, c) for n, c in files.items()]

    with patch("scripts.calibration.compute_delta.Path.glob") as mock_glob:
        mock_glob.return_value = paths
        scores = iter_experiment_scores(_fake_data_dir(paths))
    assert scores == {"case_a": [0.4], "case_b": [0.5]}


def test_iter_experiment_scores_orders_by_round_not_value() -> None:
    """轮次时序语义（I-3）：乱序分数按轮号排序取相邻差——δ 与按值排序法不同。"""
    # 轮号时序 [0.6, 0.4, 0.9] → Δ=[0.2, 0.5]；值排序 [0.4, 0.6, 0.9] → Δ=[0.2, 0.3]
    files = {
        "case_a_r1_baseline.json": json.dumps({"score": 0.6}),
        "case_a_r2.json": json.dumps({"score": 0.4}),
        "case_a_r3.json": json.dumps({"score": 0.9}),
    }
    paths = [_fake_path(n, c) for n, c in files.items()]

    with patch("scripts.calibration.compute_delta.Path.glob") as mock_glob:
        mock_glob.return_value = paths
        scores = iter_experiment_scores(_fake_data_dir(paths))
    assert scores == {"case_a": [0.6, 0.4, 0.9]}  # 按轮号时序，非按值排序
    # 10 case（case_a 2 个 Δ + 9 case × 2 个 Δ = 20 个 Δ 样本）→ 时序 δ ≠ 值排序 δ
    base = {f"c{i}": [0.0, 0.1, 0.2] for i in range(9)}
    delta_round = compute_delta_from_scores({"case_a": [0.6, 0.4, 0.9], **base})
    delta_value = compute_delta_from_scores({"case_a": [0.4, 0.6, 0.9], **base})
    assert delta_round is not None and delta_value is not None
    assert delta_round != delta_value


def test_iter_experiment_scores_excludes_best_files() -> None:
    """best.json 不参与 δ 统计（I-3）：轮文件已含 best 轮记录，重复计入产生伪零 Δ。"""
    files = {
        "case_a_r1_baseline.json": json.dumps({"score": 0.4}),
        "case_a_r2.json": json.dumps({"score": 0.6}),
        "case_a_best.json": json.dumps({"score": 0.6}),  # == r2 的 best 轮快照（伪零 Δ 源）
        "case_b_best.json": json.dumps({"score": 0.9}),  # 仅有 best 无轮文件 → 不收录
    }
    paths = [_fake_path(n, c) for n, c in files.items()]

    with patch("scripts.calibration.compute_delta.Path.glob") as mock_glob:
        mock_glob.return_value = paths
        scores = iter_experiment_scores(_fake_data_dir(paths))
    assert scores == {"case_a": [0.4, 0.6]}  # 无伪零 Δ 样本，纯 best case 不收录


def test_compute_delta_is_two_sigma() -> None:
    """δ = 2 × std(轮间相邻 |Δ|)（零方差：δ=0）；样本不足输出 None。"""
    # 10 case × 3 轮（相邻差恒 0.1）→ 20 个 Δ 样本，std=0 → δ=0.0
    scores = {f"c{i}": [0.0, 0.1, 0.2] for i in range(10)}
    delta = compute_delta_from_scores(scores)
    assert delta == 0.0

    # 样本不足：case < 10
    assert compute_delta_from_scores({"c1": [0.0, 0.1]}) is None

    # Δ 样本 < 20 边界：10 case 但每 case 仅 1 个 Δ（10 < 20）→ None
    assert compute_delta_from_scores({f"c{i}": [0.0, 0.1] for i in range(10)}) is None


def test_compute_delta_is_two_sigma_nonzero() -> None:
    """δ=2σ 非平凡验证：Δ=[0.1,0.3]×10 → stdev=0.1026 → δ=0.2052。"""
    scores = {f"c{i}": [0.0, 0.1, 0.4] for i in range(10)}
    assert compute_delta_from_scores(scores) == 0.2052
