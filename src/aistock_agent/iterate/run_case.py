"""单案例迭代闭环 CLI —— 父进程驱动基线/变体/回放/评分/终止。"""

import argparse
import asyncio
import json
import os
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
    - 评分 >= settings.iterate_target_score（0.8）→ stopped_reason=score_reached
    - best 评分曾达标但当前轮未持续 → stopped_reason=score_then_stall（报告语义修正）
    - 达到 max_rounds（默认 settings.iterate_max_rounds）
    D4/N3：δ 校准前禁用 no_improvement 终止——评分含 LLM judge 噪声，停滞判定
    会误触发或永不触发；stalled 仅观测记录，终止性只依赖 score_reached 与 max_rounds。
    round 1 为基线（无变体），round 2+ 应用 LLM 变体。
    """
    adapter = get_adapter(agent_id)
    case = load_case(case_id)
    ground_truth = load_ground_truth(str(case["ground_truth_ref"]))
    # D-2 修复：max_rounds>=1 校验移入入口（覆盖调度器直调路径——scheduler
    # 直调 run_case 时 max_rounds 可能传 0/负数，原代码静默取默认值掩盖配置错误）。
    # 注意：不能用 `max_rounds or default`——0 会被 or 吞掉绕开校验。
    limit = max_rounds if max_rounds is not None else settings.iterate_max_rounds
    if limit < 1:
        raise ValueError(f"max_rounds 必须 >= 1，收到 {limit}")
    root = Path(repo_root) if repo_root else _default_repo_root()

    # C-3（2026-08-14）：非 git 环境判定矩阵——development 无 .git 时变体轮
    # 无法恢复基线，限制为只跑基线轮；production 无 .git 直接拒绝。
    from aistock_agent.iterate.variant_engine import _check_repo_environment

    repo_env = _check_repo_environment(root)
    if repo_env == "skip":
        logger.warning(
            "iterate_repo_skip_variant_rounds",
            case_id=case_id,
            root=str(root),
        )
        limit = 1

    # T10 Q1 修复：清理上次运行残留的实验记录，防止跨运行 r*.json 污染 best.json。
    # 同一 case 多次运行时，旧 r*.json 会被 _recompute_best 纳入重算，
    # 可能选中上次运行的高分记录写入 best.json（与本次运行不一致）。
    _cleanup_stale_experiments(case_id)

    rounds: list[dict[str, object]] = []
    best: dict[str, object] = {"round": 0, "score": 0.0, "detail": None}
    stalled = 0  # D4/N3：δ 校准前禁用 no_improvement 终止，仅观测计数
    # C11/N3/F1：连续基础设施失败（回放超时/子进程失败/轮级异常/补丁空写）计数，
    # 达 3 中止 case 防无限空转
    infra_failures = 0
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
            # T11 M3 修复：基线轮纳入 try/except——returncode=0 但输出非 JSON 时
            # _run_replay_subprocess 抛 RuntimeError，原代码不在 try/except 内会崩整个闭环。
            try:
                record = await _run_baseline(adapter.agent_id, case_id, ground_truth)
                score = record["score_detail_obj"]
            except Exception as exc:  # noqa: BLE001
                logger.error(
                    "iterate_baseline_failed",
                    case_id=case_id,
                    round=round_no,
                    error=str(exc),
                )
                score = ScoreDetail(
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    gap_analysis=f"回放子进程异常：{exc}",
                )
                record = {
                    "score": 0.0,
                    "score_detail_obj": score,
                    "gap_analysis": score.gap_analysis,
                    "is_failure": True,
                }
        else:
            try:
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
                # F1 修复：apply_variant 空写（补丁不匹配且非 __new__ 模式）视为失败轮，
                # 不进入 run_experiment_round 评估，gap 挂"变体轮异常"前缀，
                # 由下方失败轮判定处统一递增 infra_failures。
                if not written and variant.target_symbol != "__new__":
                    score = ScoreDetail(
                        0.0,
                        0.0,
                        0.0,
                        0.0,
                        gap_analysis="变体轮异常：补丁未应用",
                    )
                    record = {
                        "score": 0.0,
                        "score_detail_obj": score,
                        "gap_analysis": score.gap_analysis,
                        "is_failure": True,
                    }
                else:
                    record = await run_experiment_round(
                        adapter.agent_id, case, round_no, variant, ground_truth
                    )
                    score = record["score_detail_obj"]
            except Exception as exc:  # noqa: BLE001
                # C11/N3 修复：轮级异常不崩整个闭环，计为失败轮（豁免 stalled）；
                # infra_failures 在下方失败轮判定处统一递增（F1），此处不再单独计数。
                logger.error(
                    "iterate_round_failed",
                    case_id=case_id,
                    round=round_no,
                    error=str(exc),
                )
                score = ScoreDetail(
                    0.0,
                    0.0,
                    0.0,
                    0.0,
                    gap_analysis=f"变体轮异常：{exc}",
                )
                record = {
                    "score": 0.0,
                    "score_detail_obj": score,
                    "gap_analysis": score.gap_analysis,
                    "is_failure": True,
                }

        detail = score if isinstance(score, ScoreDetail) else None
        total = detail.total if detail else float(cast("float", record.get("score", 0.0)))

        # N3/F1 修复：失败轮不计入 rounds、不更新 best、不计入 stalled
        # （不触发"连续两轮无改善"误终止）；全部失败轮类型（回放超时/子进程失败/
        # 轮级异常/补丁空写/基线异常）统一递增 infra_failures，连续 3 次中止 case。
        # T11 M1 修复：失败轮判定改用 record["is_failure"] 显式标记，
        # 替代 gap_analysis 字符串前缀魔法耦合（run_case 与 _recompute_best 需同步前缀约定）。
        # T11 M2 修复：infra_failures 是连续计数——成功轮重置为 0（散布失败不中止）。
        if record.get("is_failure", False):
            infra_failures += 1
            if infra_failures >= 3:
                stopped_reason = "infra_failures"
                break
            continue

        infra_failures = 0  # T11 M2：连续计数——成功轮重置

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
            stalled += 1  # 仅观测累计（D4/N3：不再触发任何终止）

        logger.info(
            "iterate_round_done",
            case_id=case_id,
            round=round_no,
            score=total,
            stalled=stalled,  # D4/N3：观测字段，随轮日志输出供校准期分析
        )

        # D4/N3 修复：δ 未校准前禁用 no_improvement 终止——评分含 LLM judge 噪声，
        # total > best 的停滞判定在噪声下会误触发或永不触发；终止性只依赖
        # score_reached 与 max_rounds，stalled 仅观测记录。
        # A-3 修复：confidence=low 的 GT 不构成达标（标准答案可信度不足，
        # 高分可能是对劣质 GT 的拟合，需人工回填后再验收）。
        gt_confidence = str(ground_truth.get("confidence", "high"))
        if total >= settings.iterate_target_score and gt_confidence != "low":
            stopped_reason = "score_reached"
            break
        if cast("float", best.get("score", 0.0)) >= settings.iterate_target_score:
            # A11/N11 修复：曾达标但当前轮未持续 → 报告语义修正（不谎报"未达标"）
            stopped_reason = "score_then_stall"
            break

    # C8/N2 修复：best 轮补丁固化到 best.json（原子写），负责人可复现合入
    best_patch = _recompute_best(adapter.agent_id, case_id)
    if best_patch is not None:
        best_path = get_data_dir() / "experiments" / f"{case_id}_best.json"
        tmp = best_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(best_patch, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, best_path)

    # D13 修复：闭环跑完即标记已迭代（单一权威标记，experiments 目录可清理）。
    # infra_failures 提前中止也走这里（该 case 已尝试且失败轮不落实验记录，
    # 标记防重复尝试；若需重试可手动删除标记文件）。
    # D-1（2026-08-14）：基础设施失败 → mark_failed（退避 1/2 天后自动重试，
    # 达 3 次进 deadletter）；正常结束（score_reached/max_rounds）→ mark_iterated。
    from aistock_agent.iterate.case_builder import mark_failed, mark_iterated

    if stopped_reason == "infra_failures":
        mark_failed(case_id)
    else:
        mark_iterated(case_id)

    return {
        "agent_id": agent_id,
        "case_id": case_id,
        "best_round": best["round"],
        "best_score": best["score"],
        "best_gap_analysis": best.get("gap_analysis", ""),
        "rounds": rounds,
        "stopped_reason": stopped_reason,
    }


def _cleanup_stale_experiments(case_id: str) -> None:
    """清理上次运行残留的实验记录（T10 Q1）。

    删除 data/experiments/{case_id}_r*.json（含 _r1_baseline 和 _best），
    防止跨运行残留记录被 _recompute_best 纳入重算污染 best.json。
    """
    root = get_data_dir() / "experiments"
    if not root.exists():
        return
    for p in root.glob(f"{case_id}_r*.json"):
        try:
            p.unlink()
        except OSError:
            pass
    best = root / f"{case_id}_best.json"
    if best.exists():
        try:
            best.unlink()
        except OSError:
            pass


def _recompute_best(agent_id: str, case_id: str) -> dict[str, object] | None:
    """从 data/experiments/{case_id}_r*.json 记录重算 best 轮补丁。

    返回 best 轮的 patch 规格；无任何有效实验记录返回 None（best.json 不写）。
    Important-1（final review）：失败轮记录（is_failure=true）不入 best 候选——
    其 score=0.0 且 patch 是未应用的 LLM 规格，写入 best.json 会误导合入；
    非数值 score 记录跳过（float() 不抛 ValueError 中断 run_case）。
    T11 M1 修复：失败轮判定改用 is_failure 显式标记，替代 gap_analysis 前缀约定。
    """
    root = get_data_dir() / "experiments"
    if not root.exists():
        return None
    best: dict[str, object] | None = None
    for p in sorted(root.glob(f"{case_id}_r*.json")):
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        # 失败轮不入 best 候选（T11 M1：按 is_failure 标记过滤）
        if record.get("is_failure", False):
            continue
        try:
            score = float(cast("float", record.get("score", 0.0)))
        except (TypeError, ValueError):
            continue
        if best is None or score > float(cast("float", best.get("score", 0.0))):
            best = {"score": score, "round": record.get("round"), "patch": record.get("patch", {})}
    return best


def _as_structured(value: object) -> dict[str, object] | None:
    """回放输出中的 structured 键 → evaluator 入参（非 dict 时返回 None）。"""
    return value if isinstance(value, dict) else None


async def _run_baseline(
    agent_id: str, case_id: str, ground_truth: dict[str, object]
) -> dict[str, object]:
    """round 1 基线：子进程回放 + 评分，并落盘实验记录（I5：基线轮也写入实验记录，
    使每日报告能看到 round 1）。"""
    # 函数内 import：从 variant_engine 模块命名空间按名字取（而非 from-import 固定绑定），
    # 使测试 patch("aistock_agent.iterate.variant_engine._run_replay_subprocess") 生效。
    from aistock_agent.iterate.variant_engine import (
        _content_hash,
        _needs_replay_retry,
        _now_iso_date,
        _run_replay_subprocess,
    )

    output = await _run_replay_subprocess(agent_id, case_id, "baseline")
    # 2026-08-13：基线回放偶发失败（LLM 波动）同样重试一次，与变体轮一致。
    if _needs_replay_retry(output):
        logger.warning("iterate_baseline_replay_retry_once", agent_id=agent_id, case_id=case_id)
        output = await _run_replay_subprocess(agent_id, case_id, "baseline")
    if output.get("timed_out") or output.get("subprocess_failed"):
        # G14 修复：基线失败不落盘 r1_baseline.json——若落盘，list_pending_cases
        # 会按 {case_id}_r 前缀判"已迭代"，导致该 case 永久弃置。
        score = ScoreDetail(
            0.0,
            0.0,
            0.0,
            0.0,
            gap_analysis=(
                f"回放子进程{'超时' if output.get('timed_out') else '失败'}，本轮视为失败"
            ),
        )
        return {
            **{"score": 0.0, "score_detail_obj": score, "gap_analysis": score.gap_analysis},
            "variant": {"type": "baseline", "files": [], "instructions": ""},
            "is_failure": True,
        }
    score = await evaluate_attribution(
        str(output.get("final_response", "")),
        ground_truth,
        agent_structured=_as_structured(output.get("structured")),
    )
    record: dict[str, object] = {
        "case_id": case_id,
        "round": 1,
        "agent_id": agent_id,
        "variant": {"type": "baseline", "files": [], "instructions": ""},
        # C-5：基线轮 agent 输出全文同样落盘（评分可完全重算）
        "agent_output": str(output.get("final_response", "")),
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
        "is_failure": False,
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
