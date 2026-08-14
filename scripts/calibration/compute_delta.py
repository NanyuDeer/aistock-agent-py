"""δ=2σ 校准（五期）：读 data/experiments/{case_id}_r*.json 历史轮级 score。

裁决书 D4/N3 语义：评分含 LLM judge 噪声，no_improvement 停滞判定需 δ=2σ 置信。
- 对每个 case：轮文件 score 序列（含 _r1_baseline/_best）→ 相邻差 |Δ| → 全部 Δ 样本
- δ = 2 × std(Δ)；样本不足（case < 10 或 Δ 样本 < 20）输出"数据不足"不产出配置
用法：.venv/bin/python scripts/calibration/compute_delta.py [--data-dir data]
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def iter_experiment_scores(data_dir: Path) -> dict[str, list[float]]:
    """收集各 case 轮级 score（experiments/{case_id}_r*.json，含 _r1_baseline/_best）。

    单次 glob `experiments/*` 后按文件名过滤两族：`*_r*.json`（轮文件 + _r1_baseline）
    与 `*_best.json`（best 固化快照）；reporter 写的 `{date}_experiments.json` 附件
    不含 `_r`/`_best` 标记被天然排除。损坏文件（非法 JSON/非数值/非 dict）跳过不中断。
    """
    per_case: dict[str, list[float]] = {}
    for path in data_dir.glob("experiments/*"):
        name = path.name
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        if stem.endswith("_best"):
            case_id = stem[: -len("_best")]
        elif "_r" in stem:
            case_id = stem.split("_r")[0]
        else:
            continue  # 非轮文件（如 reporter 的 {date}_experiments.json 附件）
        try:
            score = float(json.loads(path.read_text(encoding="utf-8")).get("score", 0.0))
        except (json.JSONDecodeError, ValueError, TypeError, OSError, AttributeError):
            continue  # 损坏文件跳过（不中断整批收集）
        per_case.setdefault(case_id, []).append(score)
    for scores in per_case.values():
        scores.sort()
    return per_case


def compute_delta_from_scores(scores: dict[str, list[float]]) -> float | None:
    """δ = 2 × std(轮间相邻 |Δscore|)；样本不足返回 None。

    样本门槛：case ≥ 10 且 Δ 样本 ≥ 20——否则 no_improvement 判定无统计置信。
    """
    deltas: list[float] = []
    for case_scores in scores.values():
        for prev, cur in zip(case_scores, case_scores[1:]):
            deltas.append(abs(cur - prev))
    if len(scores) < 10 or len(deltas) < 20:
        return None
    return round(2 * statistics.stdev(deltas), 4)


def main() -> int:
    parser = argparse.ArgumentParser(description="δ=2σ 校准（五期）")
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data）")
    args = parser.parse_args()

    scores = iter_experiment_scores(Path(args.data_dir))
    delta = compute_delta_from_scores(scores)
    if delta is None:
        print(f"数据不足：case {len(scores)} 个，Δ 样本不足 20 条——不产出 δ 配置")
        return 0
    print(f"δ = 2σ = {delta}")
    print(f"样本：{len(scores)} case，Δ 样本 {sum(max(0, len(v) - 1) for v in scores.values())} 条")
    print("配置：ITERATE_NO_IMPROVE_DELTA=" + str(delta))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
