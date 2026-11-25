"""人工校准集标注模板导出（五期）：从已完成 case 挑 target 条代表样本。

挑选策略（裁决书）：agent 均衡（review/event_analyst 各 ≥3）、方向性覆盖
（bullish/bearish 各 ≥1）、judge 分数分层（低/中/高）。
用法：.venv/bin/python scripts/calibration/export_calibration_set.py [--data-dir data] [--target 10]
输出：calibration/human_scores.template.json（human 字段留空待人工回填）

数据契约（run_case/variant_engine/case_builder 实际写入，2026-08-14 确认）：
- ``experiments/{case_id}_best.json``：``{"score", "round", "patch"}``
  （``_recompute_best`` 固化，无 ground_truth_ref/attribution 字段——round 字段
  回指 best 轮实验记录，供人工标注对照 agent 输出与 judge 维度分）
- ``experiments/{case_id}_r{round}.json``：轮级实验记录（round==1 文件名为
  ``{case_id}_r1_baseline.json``，round>1 为 ``{case_id}_r{round}.json``，见
  variant_engine L450 落盘约定），含 ``agent_output``（final_response 全文）+
  ``score_detail``（direction/drivers/sectors 三维分）+ ``score`` + ``round``
- ``ground_truths/{gt_id}.json``：含 ``attribution``；gt_id 契约 = case 的
  ``ground_truth_ref`` = ``gt_{case_id}``（case_builder 前缀约定）
- ``cases/{agent_id}/{case_id}.json``：切片按 agent 归档（agent_id 权威来源——
  agent_id 本身可含下划线如 event_analyst，不能从 case_id 字符串切分提取）
- ``cases/{case_id}.iterated.json``：已迭代标记（单一权威去重事实源）
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def pick_calibration_samples(
    samples: list[dict[str, Any]], *, target: int = 10
) -> list[dict[str, Any]]:
    """挑选代表样本：agent 均衡 + 方向覆盖 + judge 分数分层。

    1) 样本不足（<= target）→ 返回全部
    2) agent 均衡：每 agent 至少配额 max(1, target//3)（不足由其余 agent 补）
    3) 方向性覆盖：bullish/bearish 各 ≥1（在每 agent 配额内优先保证）
    4) judge 分数分层：剩余配额按分数低/中/高三桶轮转补足
    """
    if len(samples) <= target:
        return list(samples)
    by_agent: dict[str, list[dict[str, Any]]] = {}
    for s in samples:
        by_agent.setdefault(str(s.get("agent_id", "unknown")), []).append(s)
    picked: list[dict[str, Any]] = []
    per_agent_min = max(1, target // 3)
    for items in by_agent.values():
        picked.extend(_pick_direction_balanced(items, min(per_agent_min, len(items))))
    quota = target - len(picked)
    if quota > 0:
        remaining = [s for s in samples if s not in picked]
        picked.extend(_pick_score_tiered(remaining, quota))
    return picked[:target]


def _pick_direction_balanced(
    items: list[dict[str, Any]], need: int
) -> list[dict[str, Any]]:
    """方向性优先：bullish/bearish 各取 1（若存在），剩余按分数升序补足。"""
    chosen: list[dict[str, Any]] = []
    for direction in ("bullish", "bearish"):
        for s in items:
            if s.get("gt_attribution", {}).get("direction") == direction and s not in chosen:
                chosen.append(s)
                break
    if len(chosen) < need:
        rest = sorted(
            (s for s in items if s not in chosen),
            key=lambda s: float(s.get("judge_score", 0.0)),
        )
        chosen.extend(rest[: need - len(chosen)])
    return chosen[:need]


def _pick_score_tiered(
    candidates: list[dict[str, Any]], quota: int
) -> list[dict[str, Any]]:
    """分数分层补足：按 judge_score 排序后均分低/中/高三桶，逐桶轮转取 1 条。

    轮转（而非一次性取空低分桶）保证三档分数覆盖，避免补足阶段全落在低分档。
    """
    ordered = sorted(candidates, key=lambda s: float(s.get("judge_score", 0.0)))
    if not ordered:
        return []
    bucket_size = len(ordered) // 3
    if bucket_size == 0:
        return ordered[:quota]
    buckets = [
        ordered[:bucket_size],
        ordered[bucket_size : 2 * bucket_size],
        ordered[2 * bucket_size :],
    ]
    picked: list[dict[str, Any]] = []
    idx = 0
    while len(picked) < quota:
        progressed = False
        for bucket in buckets:
            if idx < len(bucket) and len(picked) < quota:
                picked.append(bucket[idx])
                progressed = True
        if not progressed:
            break
        idx += 1
    return picked[:quota]


def _collect_samples(base: Path) -> list[dict[str, Any]]:
    """从 experiments/best + 轮级实验记录 + ground_truths 组装候选样本。

    标注模板字段：
    - gt_attribution：GT 标准答案方向（ground_truths/{gt_id}.json 的 attribution），
      人工评分对照基准
    - agent_output：best 轮 agent 输出全文（按 best.round 匹配轮级实验记录），
      人工判断输出质量/方向时参考
    - judge_score_detail：best 轮 judge 三维分（direction/drivers/sectors），
      与 human 空字段一一对应，供人工参考 judge 分档后打分
    - human：四空字段待人工回填（direction/drivers/sectors/confidence）

    组装规则：
    - 只收已迭代 case（``cases/{case_id}.iterated.json`` 存在）
    - judge_score = best.json 的 score（run_case ``_recompute_best`` 写入）
    - agent_id 按 case_id 实际形态 ``case_{YYYYMMDD}_{agent_id}_{slug}`` 提取
    - gt_id 优先读 case 文件 ground_truth_ref，缺失按 ``gt_{case_id}`` 前缀约定推导
    - 损坏文件跳过，不中断整批组装
    """
    samples: list[dict[str, Any]] = []
    for best_path in sorted(base.glob("experiments/*_best.json")):
        case_id = best_path.name[: -len("_best.json")]
        iterated = base / "cases" / f"{case_id}.iterated.json"
        if not iterated.exists():
            continue
        try:
            best = json.loads(best_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(best, dict):
            continue
        gt_id, agent_id = _resolve_case_meta(base, case_id)
        gt_path = base / "ground_truths" / f"{gt_id}.json"
        try:
            gt = json.loads(gt_path.read_text(encoding="utf-8")) if gt_path.exists() else {}
        except (json.JSONDecodeError, OSError):
            gt = {}
        agent_output, judge_score_detail = _load_round_record(base, case_id, best)
        try:
            judge_score = float(best.get("score", 0.0))
        except (TypeError, ValueError):
            continue  # score 非数值（如字符串）→ 视为损坏，跳过该文件（与"损坏跳过"一致）
        samples.append(
            {
                "case_id": case_id,
                "gt_id": gt_id,
                "agent_id": agent_id,
                "gt_attribution": gt.get("attribution", {}) if isinstance(gt, dict) else {},
                # best.json 契约 {score, round, patch} 无 attribution 字段——
                # 保留键兼容，值恒 {}（数据源无该字段，见模块 docstring 数据契约）
                "agent_best_attribution": {},
                "agent_output": agent_output,
                "judge_score_detail": judge_score_detail,
                "judge_score": judge_score,
                "human": {
                    "direction_score": None,
                    "drivers_score": None,
                    "sectors_score": None,
                    "confidence": None,
                },
            }
        )
    return samples


def _load_round_record(
    base: Path, case_id: str, best: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    """按 best 轮号加载轮级实验记录，取 agent_output 全文 + judge_score_detail 三维分。

    best.json 的 round 由 ``_recompute_best`` 固化（run_case.py L342）：
    round==1 → ``experiments/{case_id}_r1_baseline.json``，round>1 →
    ``experiments/{case_id}_r{round}.json``（variant_engine L450 落盘约定）。
    记录缺失/损坏/字段类型异常 → 降级 ("", {})——人工标注仍可基于
    gt_attribution 评分，不阻断整批组装。
    """
    round_no = best.get("round")
    exp_path = (
        base / "experiments" / f"{case_id}_r1_baseline.json"
        if round_no == 1
        else base / "experiments" / f"{case_id}_r{round_no}.json"
    )
    try:
        record = json.loads(exp_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return "", {}
    if not isinstance(record, dict):
        return "", {}
    raw_output = record.get("agent_output", "")
    raw_detail = record.get("score_detail", {})
    return (
        raw_output if isinstance(raw_output, str) else "",
        raw_detail if isinstance(raw_detail, dict) else {},
    )


def _resolve_case_meta(base: Path, case_id: str) -> tuple[str, str]:
    """解析 case 文件元数据 (gt_id, agent_id)。

    - gt_id 优先取 case 文件 ground_truth_ref（case_builder 构造切片时硬编码
      ``ground_truth_ref = f"gt_{case_id}"``），缺失按该前缀约定推导——
      best.json 不含该字段，不能从 best 读取。
    - agent_id 取 case 文件所在目录名（case 按 agent 归档，见 case_builder）。
      不能从 case_id 按 "_" 切分提取——agent_id 本身可含下划线
      （event_analyst），``split("_")[2]`` 会截断为 "event"；目录名是权威来源。
      切片缺失时 fallback 字符串启发式（对不含下划线的 agent_id 有效）。
    """
    fallback_agent = case_id.split("_")[2] if case_id.count("_") >= 2 else "unknown"
    for p in base.glob("cases/**/*.json"):
        if p.name != f"{case_id}.json":
            continue  # 跳过 iterated 标记等非切片文件
        try:
            payload = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            break
        gt_ref = ""
        if isinstance(payload, dict):
            ref = payload.get("ground_truth_ref")
            if isinstance(ref, str) and ref:
                gt_ref = ref
        return gt_ref or f"gt_{case_id}", p.parent.name
    return f"gt_{case_id}", fallback_agent


def main() -> int:
    parser = argparse.ArgumentParser(description="人工校准集标注模板导出（五期）")
    parser.add_argument("--data-dir", default="data", help="数据目录（默认 data）")
    parser.add_argument("--target", type=int, default=10, help="目标样本数（默认 10）")
    args = parser.parse_args()

    base = Path(args.data_dir)
    samples = _collect_samples(base)
    picked = pick_calibration_samples(samples, target=args.target)
    out_dir = base / "calibration"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "human_scores.template.json"
    out_path.write_text(json.dumps(picked, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"导出 {len(picked)} 条标注模板 → {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
