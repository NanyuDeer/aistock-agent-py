"""judge bias 对比报告（五期）：对照人工标注量化 LLM judge 的逐维度偏差。

输入 calibration/human_scores.json（Task 3 模板人工回填）：
每条含 judge_score_detail（三维 0-1）+ human{direction_score/drivers_score/sectors_score}。
输出 calibration/bias_report.md：
- 逐维度 MAD（平均绝对差）与 signed 平均偏差（正 = judge 偏高）
- 按 GT 方向分组的偏差（bullish/bearish/neutral 是否系统性低估/高估）
- 结论区（供裁决：是否调整 judge prompt / 权重序重估依据）
只产出报告，不自动改 evaluator。
用法：.venv/bin/python scripts/calibration/report_judge_bias.py [--data-dir data]
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

_GT_DIRECTIONS = ("bullish", "bearish", "neutral")


def _resolve_gt_direction(row: dict[str, Any]) -> str:
    """从行记录解析归一化 GT 方向（顶层 gt_direction 优先，其次 gt_attribution.direction）。

    Task 3 模板无顶层 gt_direction 字段（方向在 gt_attribution.direction），
    这里归一化补全；白名单外/缺失 → "unknown"，供分组偏差展示不丢弃样本。
    """
    raw: Any = row.get("gt_direction")
    if not isinstance(raw, str) or not raw:
        attr = row.get("gt_attribution")
        raw = attr.get("direction") if isinstance(attr, dict) else None
    if isinstance(raw, str) and raw in _GT_DIRECTIONS:
        return raw
    return "unknown"


def compute_dimension_bias(
    rows: list[dict[str, Any]], *, group_by: str | None = None
) -> dict[str, Any]:
    """逐维度（direction/drivers/sectors）MAD + signed 平均偏差；可选按字段分组。

    signed 为正 = judge 偏高（人工标注为基准）。维度分值缺值（None/非数值，
    Task 3 模板 human 初始 None）跳过该行该维——不当作 0 以免扭曲偏差。
    """
    if group_by:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            key = str(row.get(group_by, "unknown"))
            grouped.setdefault(key, []).append(row)
        return {key: compute_dimension_bias(items) for key, items in grouped.items()}
    dims = ("direction", "drivers", "sectors")
    result: dict[str, dict[str, float]] = {}
    for dim in dims:
        diffs: list[float] = []
        for row in rows:
            detail = row.get("judge_score_detail")
            human = row.get("human")
            judge = detail.get(dim) if isinstance(detail, dict) else None
            human_v = human.get(f"{dim}_score") if isinstance(human, dict) else None
            if isinstance(judge, int | float) and isinstance(human_v, int | float):
                diffs.append(float(judge) - float(human_v))
        result[dim] = {
            "mad": round(statistics.fmean(abs(d) for d in diffs), 4) if diffs else 0.0,
            "signed": round(statistics.fmean(diffs), 4) if diffs else 0.0,
        }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="judge bias 对比报告（五期）")
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data）")
    args = parser.parse_args()

    base = Path(args.data_dir)
    src = base / "calibration" / "human_scores.json"
    if not src.exists():
        print(f"未找到标注文件 {src}——请先用 export_calibration_set.py 导出模板并人工回填")
        return 0
    rows = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        print(f"标注文件 {src} 格式异常：应为 JSON 数组")
        return 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        row["gt_direction"] = _resolve_gt_direction(row)
    overall = compute_dimension_bias(rows)
    by_dir = compute_dimension_bias(rows, group_by="gt_direction")

    out = base / "calibration" / "bias_report.md"
    lines = [
        "# Judge Bias 对比报告（五期）",
        f"\n样本：{len(rows)} 条（human_scores.json）",
        "\n## 逐维度偏差（MAD / signed，正 = judge 偏高）",
        "\n| 维度 | MAD | 平均偏差 |",
        "|------|-----|---------|",
    ]
    for dim, stats in overall.items():
        lines.append(f"| {dim} | {stats['mad']} | {stats['signed']} |")
    lines.append("\n## 按 GT 方向分组（signed 偏差）")
    for direction, dims in by_dir.items():
        parts = ", ".join(f"{d}={v['signed']}" for d, v in dims.items())
        lines.append(f"- {direction}: {parts}")
    lines.append("\n## 结论（待裁决）\n")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"bias 报告已生成 → {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
