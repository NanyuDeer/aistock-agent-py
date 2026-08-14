"""event 达标线评估报告（五期）：统计已迭代 event_analyst case 的达标情况。

数据契约：stopped_reason 不落盘（iterated 标记仅 status/round_type/retry_count/
iterated_at；run_case 返回值未持久化）——达标判定数据驱动：
达标 = best_score >= target 且 GT confidence != "low"（A-3 语义：low GT 不构成达标）。
数据源：
- data/experiments/{case_id}_best.json（{score, round, patch}）
- data/ground_truths/gt_{case_id}.json（confidence；gt_id 前缀与 case_builder
  L94 ground_truth_ref=`gt_{case_id}` 一致）
- data/cases/{case_id}.iterated.json（status=iterated 才统计）
rounds 用 best_round（best.json 的 round 字段）。
只产出报告，不自动改达标线。
用法：.venv/bin/python scripts/calibration/report_event_attainment.py
      [--data-dir data] [--target 0.8] [--max-rounds 5]
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

from aistock_agent.config import settings


def _is_event_case(case_id: str) -> bool:
    """event_analyst case 判断（case_id 前缀：case_{YYYYMMDD}_event_analyst_）。"""
    return "_event_analyst_" in case_id


def _gt_confidence(base: Path, gt_ref: str) -> str:
    """读取 GT confidence（缺失/损坏/非 dict → "unknown"）。"""
    try:
        gt = json.loads((base / "ground_truths" / f"{gt_ref}.json").read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "unknown"
    if not isinstance(gt, dict):
        return "unknown"  # 合法 JSON 但顶层非 dict（如数组）→ 视为损坏，跳过
    return str(gt.get("confidence", "unknown"))


def _collect_event_cases(base: Path) -> list[dict[str, Any]]:
    """收集 event_analyst 已迭代 case（iterated 标记 + best + GT confidence）。

    过滤链：experiments/*_best.json → _is_event_case（case_id 含
    _event_analyst_）→ cases/{case_id}.iterated.json 存在且 status=iterated
    → best.json 合法（损坏跳过）→ 追加 {case_id, best_score, best_round,
    gt_confidence}。GT 文件缺失/损坏 → confidence=unknown（!= low，按
    非 low 计入达标判定，与 A-3 语义一致）。
    """
    cases: list[dict[str, Any]] = []
    for best_path in base.glob("experiments/*_best.json"):
        case_id = best_path.name[: -len("_best.json")]
        if not _is_event_case(case_id):
            continue
        iterated = base / "cases" / f"{case_id}.iterated.json"
        try:
            mark = json.loads(iterated.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(mark, dict):
            continue  # 合法 JSON 但顶层非 dict（如数组）→ 视为损坏，跳过
        if mark.get("status") != "iterated":
            continue
        try:
            best = json.loads(best_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(best, dict):
            continue  # 合法 JSON 但顶层非 dict（如数组）→ 视为损坏，跳过
        cases.append({
            "case_id": case_id,
            "best_score": float(best.get("score", 0.0)),
            "best_round": int(best.get("round", 0)),
            "gt_confidence": _gt_confidence(base, f"gt_{case_id}"),
        })
    return cases


def compute_event_attainment(
    cases: list[dict[str, Any]], *, target_score: float, max_rounds: int
) -> dict[str, float]:
    """达标率 + 轮数分布（达标 = best_score >= target 且 GT confidence != low）。

    max_rounds_exhausted：best_round >= max_rounds 的 case 数（best_round 即
    best.json 的 round，表示实验轮数已达上限耗尽）。空输入 → 全零统计。
    """
    if not cases:
        return {
            "attainment_rate": 0.0,
            "avg_rounds": 0.0,
            "median_rounds": 0.0,
            "max_rounds_exhausted": 0.0,
        }
    attained = [
        c for c in cases
        if c["best_score"] >= target_score and c.get("gt_confidence") != "low"
    ]
    rounds = [c["best_round"] for c in cases]
    return {
        "attainment_rate": round(len(attained) / len(cases), 4),
        "avg_rounds": round(statistics.fmean(rounds), 4),
        "median_rounds": round(statistics.median(rounds), 4),
        "max_rounds_exhausted": sum(1 for r in rounds if r >= max_rounds),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="event 达标线评估报告（五期）")
    parser.add_argument("--data-dir", default=settings.iterate_data_dir,
                        help="数据目录（默认 settings.iterate_data_dir）")
    parser.add_argument("--target", type=float, default=settings.iterate_target_score,
                        help="达标线（默认 settings.iterate_target_score）")
    parser.add_argument("--max-rounds", type=int, default=settings.iterate_max_rounds,
                        help="max_rounds（默认 settings.iterate_max_rounds）")
    args = parser.parse_args()

    base = Path(args.data_dir)
    cases = _collect_event_cases(base)
    stats = compute_event_attainment(cases, target_score=args.target, max_rounds=args.max_rounds)

    out = base / "calibration" / "event_attainment_report.md"
    # 数据目录可能不存在（空数据冒烟）——先建 calibration/ 再落盘，保证 exit 0
    out.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# Event 达标线评估报告（五期）",
        f"\n已迭代 event_analyst case：{len(cases)} 条"
        f"（达标线 {args.target}，max_rounds {args.max_rounds}）",
        f"\n- 达标率（best_score >= {args.target} 且 GT confidence != low）："
        f"{stats['attainment_rate']}",
        f"- 平均轮数：{stats['avg_rounds']}",
        f"- 中位轮数：{stats['median_rounds']}",
        f"- max_rounds 耗尽 case 数：{int(stats['max_rounds_exhausted'])}",
        "\n## 结论（待裁决，不自动改达标线）\n",
    ]
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"达标率报告已生成 → {out}（{len(cases)} 条 case）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
