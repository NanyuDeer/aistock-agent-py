"""build_iterate_cases.py — 迭代切片生成 CLI（二期）。

用法：
  python scripts/build_iterate_cases.py --agent review [--force] [--data-dir PATH]
  python scripts/build_iterate_cases.py --agent event_analyst \
      --window-days 30 [--force] [--data-dir PATH]

review：自动发现最近交易日 → build_market_trace_snapshot 真实收盘快照 → case + 数据约束 GT + 校验
event：扫描 window-days 天内电报重大事件 → 每事件 case + GT + 校验
只在服务器沙盒/生产环境运行（依赖 Node 生产数据源与 LLM key）。
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import cast

import structlog

logger = structlog.get_logger()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aistock_agent.iterate.adapters import get_adapter  # noqa: E402
from aistock_agent.iterate.case_builder import (  # noqa: E402
    build_case,
    case_path,
    get_data_dir,
)
from aistock_agent.iterate.case_scanner import (  # noqa: E402
    find_recent_trading_day,
    scan_major_events,
)
from aistock_agent.iterate.ground_truth import generate_data_constrained_gt  # noqa: E402
from aistock_agent.iterate.gt_validator import validate_gt_against_case  # noqa: E402
from aistock_agent.services.market_trace_snapshot import build_market_trace_snapshot  # noqa: E402


def _source_to_record(source: dict[str, object]) -> dict[str, object]:
    """SourceRecord dict → build_case 的 telegraph_records 形状。"""
    occurred = source.get("occurred_at")
    return {
        "time": str(occurred) if occurred else "",
        "title": str(source.get("title", "")),
        "content": str(source.get("content", "")),
        "url": str(source.get("url", "")),
    }


async def build_review_case(
    *,
    data_dir: Path,
    force: bool,
    snapshot: object | None = None,
) -> dict[str, object]:
    """构建 review 真实切片（snapshot 参数供测试注入）。"""
    if snapshot is None:
        day = await find_recent_trading_day()
        if day is None:
            raise RuntimeError("无法发现最近交易日（Node close-snapshot/last-close 均失败）")
        snapshot = await build_market_trace_snapshot(day)

    trade_date = str(getattr(snapshot, "trade_date", ""))
    captured_at = getattr(snapshot, "captured_at", None)
    discovery = getattr(snapshot, "phenomenon_discovery", None)
    primary = getattr(discovery, "primary", None)
    event_title = str(getattr(primary, "summary", "")) or f"A股收盘{trade_date}"

    snapshot_dict = snapshot.model_dump(mode="json")  # type: ignore[attr-defined]
    sources = snapshot_dict.get("sources", {})
    telegraph_records = [
        _source_to_record(cast("dict[str, object]", src))
        for src in sources.values()
        if isinstance(src, dict) and src.get("kind") in {"event_evidence", "market_fact"}
    ]

    case = await build_case(
        get_adapter("review"),
        event_title=event_title,
        event_time=captured_at,
        telegraph_records=telegraph_records,
        market_snapshot=snapshot_dict,
        data_dir=data_dir,
    )
    case["meta"] = {"snapshot_kind": "full", "t_window": "close"}

    gt = await generate_data_constrained_gt(case, data_dir=data_dir)
    violations = validate_gt_against_case(gt, case)
    if violations and not force:
        _rollback(case, gt, data_dir)
        raise RuntimeError(f"review case 校验拒绝：{violations}")
    return {
        "case_id": str(case["case_id"]),
        "rejected": bool(violations),
        "reasons": violations,
    }


async def build_event_cases(
    *,
    events: list[dict[str, object]],
    data_dir: Path,
    force: bool,
) -> dict[str, object]:
    """构建 event_analyst 切片（events 参数供测试注入；CLI 默认走 scan_major_events）。"""
    from datetime import datetime as _dt

    adapter = get_adapter("event_analyst")
    case_ids: list[str] = []
    rejected = 0
    reasons: list[str] = []
    for event in events:
        event_time = _dt.fromisoformat(str(event["event_time"]))
        case = await build_case(
            adapter,
            event_title=str(event["event_title"]),
            event_time=event_time,
            telegraph_records=cast("list[dict[str, object]]", event["telegraph_records"]),
            data_dir=data_dir,
        )
        case["meta"] = {"t_window": "event"}
        gt = await generate_data_constrained_gt(case, data_dir=data_dir)
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


def _rollback(case: dict[str, object], gt: dict[str, object], data_dir: Path) -> None:
    """校验失败时删除已落盘的 case 与 GT 文件。"""
    try:
        case_path(str(case["case_id"]), data_dir=data_dir).unlink(missing_ok=True)
        gt_path = data_dir / "ground_truths" / f"{gt['gt_id']}.json"
        gt_path.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        logger.warning("iterate_case_rollback_failed", exc_info=True)


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="迭代切片生成")
    parser.add_argument("--agent", required=True, choices=("review", "event_analyst"))
    parser.add_argument("--window-days", type=int, default=30)
    parser.add_argument("--force", action="store_true", help="跳过一致性校验强制落盘")
    parser.add_argument("--data-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    data_dir = args.data_dir or get_data_dir()

    if args.agent == "review":
        result = await build_review_case(data_dir=data_dir, force=args.force)
        print(
            f"review case 生成：{result['case_id']}"
            f"（校验{'拒绝' if result['rejected'] else '通过'}）"
        )
        if result["reasons"]:
            print("原因：", *result["reasons"], sep="\n  - ")
    else:
        events = await scan_major_events(args.window_days)
        if not events:
            print(f"近 {args.window_days} 天未发现重大事件")
            return 0
        result = await build_event_cases(
            events=events, data_dir=data_dir, force=args.force
        )
        print(f"event case 生成：{result['generated']} 个，拒绝 {result['rejected']} 个")
        for r in result["reasons"]:
            print(f"  - {r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
