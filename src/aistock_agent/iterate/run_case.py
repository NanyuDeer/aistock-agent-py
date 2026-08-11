"""单案例迭代闭环 CLI —— 父进程驱动基线/变体/回放/评分/终止。"""

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import cast

import structlog

from aistock_agent.config import settings
from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_builder import get_data_dir, load_case
from aistock_agent.iterate.evaluator import ScoreDetail, evaluate_attribution
from aistock_agent.iterate.ground_truth import load_ground_truth
from aistock_agent.iterate.variant_engine import (
    apply_variant,
    generate_variant,
    restore_baseline,
    run_experiment_round,
)

logger = structlog.get_logger()


async def run_case(
    agent_id: str,
    case_id: str,
    *,
    max_rounds: int | None = None,
    repo_root: str | None = None,
) -> dict[str, object]:
    """对单个案例跑迭代闭环。

    终止条件：
    - 评分 >= settings.iterate_target_score（0.8）
    - 连续两轮评分未上升（no_improvement）
    - 达到 max_rounds（默认 settings.iterate_max_rounds）
    round 1 为基线（无变体），round 2+ 应用 LLM 变体。
    """
    adapter = get_adapter(agent_id)
    case = load_case(case_id)
    ground_truth = load_ground_truth(str(case["ground_truth_ref"]))
    limit = max_rounds or settings.iterate_max_rounds
    root = Path(repo_root) if repo_root else _default_repo_root()

    rounds: list[dict[str, object]] = []
    best: dict[str, object] = {"round": 0, "score": 0.0, "detail": None}
    stalled = 0
    stopped_reason = "max_rounds"
    # I2：上一轮 apply_variant 实际写过的文件（相对仓库根路径），下一轮开始时
    # 连同 adapter 声明的文件一起 restore_baseline，防止 data_source_diff 改动
    # tools/config 等未声明文件跨轮残留。
    last_written: tuple[str, ...] = ()

    for round_no in range(1, limit + 1):
        restore_baseline(adapter, root, extra_files=last_written)

        if round_no == 1:
            variant = None
            # 基线：直接回放评估（无变体）
            record = await _run_baseline(adapter.agent_id, case_id, ground_truth)
            score = record["score_detail_obj"]
        else:
            variant = await generate_variant(
                adapter,
                case,
                ground_truth,
                _last_score(best),
                str(best.get("gap_analysis", "")),
                root,
            )
            written = apply_variant(variant, root)
            last_written = tuple(str(p.relative_to(root.resolve())) for p in written)
            record = await run_experiment_round(
                adapter.agent_id, case, round_no, variant, ground_truth
            )
            score = record["score_detail_obj"]

        detail = score if isinstance(score, ScoreDetail) else None
        total = detail.total if detail else float(cast("float", record.get("score", 0.0)))
        rounds.append(
            {
                "round": round_no,
                "variant_type": variant.type if variant else "baseline",
                "score": total,
                "gap_analysis": detail.gap_analysis if detail else "",
            }
        )
        if total > cast("float", best.get("score", 0.0)):
            best = {
                "round": round_no,
                "score": total,
                "detail": detail,
                "gap_analysis": detail.gap_analysis if detail else "",
            }
            stalled = 0
        else:
            stalled += 1

        logger.info("iterate_round_done", case_id=case_id, round=round_no, score=total)

        if total >= settings.iterate_target_score:
            stopped_reason = "score_reached"
            break
        if stalled >= 2:
            stopped_reason = "no_improvement"
            break

    return {
        "agent_id": agent_id,
        "case_id": case_id,
        "best_round": best["round"],
        "best_score": best["score"],
        "best_gap_analysis": best.get("gap_analysis", ""),
        "rounds": rounds,
        "stopped_reason": stopped_reason,
    }


async def _run_baseline(
    agent_id: str, case_id: str, ground_truth: dict[str, object]
) -> dict[str, object]:
    """round 1 基线：子进程回放 + 评分，并落盘实验记录（I5：基线轮也写入实验记录，
    使每日报告能看到 round 1）。"""
    # 函数内 import：从 variant_engine 模块命名空间按名字取（而非 from-import 固定绑定），
    # 使测试 patch("aistock_agent.iterate.variant_engine._run_replay_subprocess") 生效。
    from aistock_agent.iterate.variant_engine import (
        _content_hash,
        _now_iso_date,
        _run_replay_subprocess,
    )

    output = await _run_replay_subprocess(agent_id, case_id, "baseline")
    if output.get("timed_out"):
        score = ScoreDetail(
            0.0,
            0.0,
            0.0,
            0.0,
            gap_analysis=(
                f"回放子进程超时（>{settings.iterate_round_timeout_seconds}s），本轮视为失败"
            ),
        )
    else:
        score = await evaluate_attribution(str(output.get("final_response", "")), ground_truth)
    record: dict[str, object] = {
        "case_id": case_id,
        "round": 1,
        "agent_id": agent_id,
        "variant": {"type": "baseline", "files": [], "instructions": ""},
        "score": score.total,
        "score_detail": {
            "direction": score.direction,
            "drivers": score.drivers,
            "sectors": score.sectors,
        },
        "gap_analysis": score.gap_analysis,
        "duration_ms": 0,
        "variant_hash": _content_hash({}),
        "created_at": _now_iso_date(),
    }
    path = get_data_dir() / "experiments" / f"{case_id}_r1_baseline.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return {**record, "score_detail_obj": score}


def _last_score(best: dict[str, object]) -> ScoreDetail | None:
    detail = best.get("detail")
    return detail if isinstance(detail, ScoreDetail) else None


def _default_repo_root() -> Path:
    """src/aistock_agent/iterate/run_case.py 上溯 4 层 = 仓库根。"""
    return Path(__file__).resolve().parent.parent.parent.parent


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Run iterate closed-loop for one case")
    parser.add_argument("agent_id")
    parser.add_argument("case_id")
    parser.add_argument("--max-rounds", type=int, default=None)
    args = parser.parse_args(argv)
    result = asyncio.run(run_case(args.agent_id, args.case_id, max_rounds=args.max_rounds))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
