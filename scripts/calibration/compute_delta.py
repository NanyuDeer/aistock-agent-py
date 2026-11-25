"""δ=2σ 校准（五期）：读 data/experiments/{case_id}_r{round}.json 历史轮级 score。

裁决书 D4/N3 语义：评分含 LLM judge 噪声，no_improvement 停滞判定需 δ=2σ 置信。
- 对每个 case：轮文件 score 按**轮号时序**（_r1_baseline/_r{round}，非按值排序）
  → 相邻差 |Δ| → 全部 Δ 样本
- best.json 固化快照**不参与** δ 统计（轮文件已含 best 轮记录，重复计入会产生
  伪零 Δ）
- δ = 2 × std(Δ)；样本不足（case < 10 或 Δ 样本 < 20）输出"数据不足"不产出配置
用法：.venv/bin/python scripts/calibration/compute_delta.py [--data-dir data]
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from pathlib import Path

# 轮文件后缀：round==1 为 {case_id}_r1_baseline.json，round>1 为 {case_id}_r{round}.json
# （variant_engine L450 落盘约定）；锚定末尾避免误匹配 case_id 内嵌的 "_r"。
_ROUND_FILE_RE = re.compile(r"_r(\d+)(?:_baseline)?$")


def iter_experiment_scores(data_dir: Path) -> dict[str, list[float]]:
    """收集各 case 轮级 score（experiments/{case_id}_r{round}.json 轮文件，按轮号时序）。

    单次 glob `experiments/*` 后按文件名过滤**轮文件族**（`*_r{round}.json`，round==1
    为 `_r1_baseline`）；`*_best.json` 固化快照**排除**——轮文件已含 best 轮记录，
    重复计入会产生伪零 Δ；reporter 写的 `{date}_experiments.json` 附件不含轮号后缀
    被天然排除。轮文件按轮号排序（而非按 score 值排序）——Δ 是轮次时序上的相邻差。
    损坏文件（非法 JSON/非数值/非 dict）跳过不中断。
    """
    per_case: dict[str, list[tuple[int, float]]] = {}
    for path in data_dir.glob("experiments/*"):
        name = path.name
        if not name.endswith(".json"):
            continue
        stem = name[: -len(".json")]
        if stem.endswith("_best"):
            continue  # best 固化快照不参与 δ 统计（轮文件已含 best 轮记录）
        m = _ROUND_FILE_RE.search(stem)
        if m is None:
            continue  # 非轮文件（如 reporter 的 {date}_experiments.json 附件）
        try:
            score = float(json.loads(path.read_text(encoding="utf-8")).get("score", 0.0))
        except (json.JSONDecodeError, ValueError, TypeError, OSError, AttributeError):
            continue  # 损坏文件跳过（不中断整批收集）
        case_id = stem[: m.start()]
        per_case.setdefault(case_id, []).append((int(m.group(1)), score))
    # 按轮号时序排序（glob 顺序/落盘顺序不保证时序，值排序更非轮次语义）
    return {
        case_id: [score for _, score in sorted(rounds)]
        for case_id, rounds in per_case.items()
    }


def compute_delta_from_scores(scores: dict[str, list[float]]) -> float | None:
    """δ = 2 × std(轮次时序相邻 |Δscore|)；样本不足返回 None。

    入参 scores 的每个 list 按轮号时序排列（见 iter_experiment_scores）——相邻差即
    轮次推进的 score 变化。样本门槛：case ≥ 10 且 Δ 样本 ≥ 20——否则 no_improvement
    判定无统计置信。
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
