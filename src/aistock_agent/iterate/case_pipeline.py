"""case_pipeline.py — 通用产片流水线（二期 case-sourcing）。

build_cases_for_adapter 是产片的唯一入口：sourcing → 逐候选 build_case →
GT → 校验 → 回滚。不感知具体 agent（分派事实源是 adapter.case_sources）。
"""

from pathlib import Path
from typing import Any, cast

import structlog

from aistock_agent.iterate.adapters import IterableAgentAdapter
from aistock_agent.iterate.case_builder import build_case, case_path
from aistock_agent.iterate.case_sourcers import CaseCandidate, source_cases
from aistock_agent.iterate.ground_truth import generate_data_constrained_gt
from aistock_agent.iterate.gt_validator import validate_gt_against_case

logger = structlog.get_logger()

#: data_deps 切片字段 → CaseCandidate 可提供字段的映射（校验集合）
_DEPS_TO_CANDIDATE: dict[str, str] = {
    "cls_telegraph": "telegraph_records",
    "market_snapshot": "market_snapshot",
    "global_markets": "market_snapshot",  # build_case 内部由 market_snapshot 派生
    "industry_graph": "industry_graph",
}


def candidate_to_case_inputs(
    adapter: IterableAgentAdapter, candidate: CaseCandidate
) -> dict[str, object]:
    """CaseCandidate → build_case 关键字参数；data_deps 覆盖字段缺失即抛错。

    构建期快速失败：空壳切片（缺 data_deps 依赖）不得进入闭环（一期
    case_20260731_us_market_surge 全 0 分事故防线）。
    """
    for dep, field_name in _DEPS_TO_CANDIDATE.items():
        if dep not in adapter.data_deps.values():
            continue
        if field_name == "market_snapshot" and candidate.market_snapshot is None:
            raise ValueError(f"candidate 缺 data_deps 字段 market_snapshot（adapter={adapter.agent_id}）")  # noqa: E501
        if field_name == "industry_graph" and candidate.industry_graph is None:
            raise ValueError(f"candidate 缺 data_deps 字段 industry_graph（adapter={adapter.agent_id}）")  # noqa: E501
    return {
        "event_title": candidate.event_title,
        "event_time": candidate.event_time,
        "telegraph_records": candidate.telegraph_records,
        "market_snapshot": candidate.market_snapshot,
        "industry_graph": candidate.industry_graph,
        "meta": candidate.meta,
    }


async def build_cases_for_adapter(
    adapter: IterableAgentAdapter,
    *,
    data_dir: Path,
    force: bool = False,
) -> dict[str, object]:
    """通用产片：sourcing → 逐候选 build_case → GT → 校验 → 回滚。

    返回 {"generated", "rejected", "case_ids", "reasons"}（与旧 build_event_cases 形状一致）。
    单候选失败仅回滚该候选，不阻断后续。
    """
    candidates = await source_cases(adapter, data_dir=data_dir, force=force)
    case_ids: list[str] = []
    rejected = 0
    reasons: list[str] = []
    for candidate in candidates:
        try:
            case = await build_case(
                adapter,
                data_dir=data_dir,
                **cast("Any", candidate_to_case_inputs(adapter, candidate)),
            )
        except ValueError as exc:
            rejected += 1
            reasons.append(str(exc))
            continue
        try:
            gt = await generate_data_constrained_gt(case, data_dir=data_dir)
        except Exception as exc:  # noqa: BLE001 — GT 生成失败（LLM 超时等）→ 回滚已落盘 case
            _rollback(case, None, data_dir)  # gt 未生成，仅回滚 case（防孤儿 case 进 pending）
            rejected += 1
            reasons.append(f"GT 生成失败: {exc}")
            continue
        violations = validate_gt_against_case(gt, case)
        if violations and not force:
            _rollback(case, gt, data_dir)
            rejected += 1
            reasons.extend(violations)
            continue
        case_ids.append(str(case["case_id"]))
    return {
        "generated": len(case_ids),
        "rejected": rejected,
        "case_ids": case_ids,
        "reasons": reasons,
    }


def _rollback(case: dict[str, object], gt: dict[str, object] | None, data_dir: Path) -> None:
    try:
        case_path(str(case["case_id"]), data_dir=data_dir).unlink(missing_ok=True)
        if gt is not None:
            gt_path = data_dir / "ground_truths" / f"{gt['gt_id']}.json"
            gt_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("iterate_case_rollback_failed", exc_info=True)
