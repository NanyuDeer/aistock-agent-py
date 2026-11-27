"""V4 实时运行 CLI：手工触发一次真实取数 run 并输出摘要。

用法：
    python -m aistock_agent.brain_v4.runtime.cli \
        --profile market_trace --date 2026-11-27 \
        [--json-out PATH] [--html-out DIR] [--archive] [--report-dir DIR]

为什么保留 CLI：P3 需要人工核验真实数据链路；P4 调度器之后复用同一 `run_live`，
CLI 只做参数解析与摘要输出，不复制任何取数/研判逻辑。`--archive` 默认关闭，
人工灰度时显式开启；`--report-dir` 生成每日静态审计页。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
from datetime import date as date_type
from pathlib import Path

from aistock_agent.brain_v4.runtime.archive import (
    ArchiveResult,
    archive_run,
    write_daily_report,
)
from aistock_agent.brain_v4.runtime.bootstrap import ensure_app_env
from aistock_agent.brain_v4.runtime.runner import LiveRunResult, run_live

_PROFILES = (
    "market_trace",
    "briefing_morning",
    "briefing_midday",
    "briefing_evening",
)


def _summary(
    result: LiveRunResult, archive: ArchiveResult | None = None
) -> dict[str, object]:
    judgment = result.judgment
    six_factor = result.six_factor
    summary: dict[str, object] = {
        "run_id": result.run_id,
        "profile": result.profile,
        "status": result.status,
        "as_of": result.as_of.isoformat(),
        "data_cutoff": result.data_cutoff.isoformat(),
        "snapshot_id": result.snapshot_id,
        "snapshot_sha": result.snapshot_sha,
        "parent_run_id": result.parent_run_id,
        "parent_judgment_ids": result.parent_judgment_ids,
        "judgment": None
        if judgment is None
        else {
            "judgment_id": judgment.judgment_id,
            "decision": judgment.decision,
            "status": judgment.status,
            "stop_reason": judgment.stop_reason,
            "confidence": judgment.confidence,
            "evidence_ids": judgment.evidence_ids,
        },
        "six_factor": None
        if six_factor is None
        else {
            "chain_id": six_factor.chain_id,
            "gate_passed": six_factor.gate_passed,
            "steps": [
                {"stage": step.stage, "status": step.status}
                for step in six_factor.steps
            ],
        },
        "usage": result.usage,
        "audit": result.audit,
    }
    if archive is not None:
        summary["archive"] = {
            "archived": archive.archived,
            "run_id": archive.run_id,
            "error": archive.error,
            "response": archive.response,
        }
    return summary


def _render_html(result: LiveRunResult, summary: dict[str, object]) -> str:
    title = f"V4 live run {result.run_id}"
    body = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    return (
        "<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
        f"<title>{html.escape(title)}</title></head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"<pre>{html.escape(body)}</pre>"
        "</body></html>"
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="V4 实时运行器")
    parser.add_argument("--profile", required=True, choices=_PROFILES)
    parser.add_argument("--date", required=True, help="交易日，ISO 格式 YYYY-MM-DD")
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--html-out", type=Path, default=None)
    parser.add_argument(
        "--archive",
        action="store_true",
        default=False,
        help="归档到 app-api 影子表（默认关闭）",
    )
    parser.add_argument(
        "--report-dir",
        type=Path,
        default=None,
        help="生成 <dir>/<date>/index.html 每日静态审计页",
    )
    return parser.parse_args(argv)


async def _run_all(args: argparse.Namespace, trading_date: date_type) -> int:
    """在同一个事件循环内依次完成 live run、归档与报告写出。

    为什么必须单循环：`HttpClientPool` 的 httpx.AsyncClient 绑定创建它的事件
    循环；若 `archive_run` 用第二个 `asyncio.run`，归档请求会在已关闭的循环上
    静默失败（`archive.error="empty_response"`）。
    """
    try:
        result = await run_live(args.profile, trading_date)
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转非零退出码
        print(f"V4 live run 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    archive: ArchiveResult | None = None
    if args.archive:
        archive = await archive_run(result)
        if archive.archived:
            print(f"archived: run_id={archive.run_id}", file=sys.stderr)
        else:
            print(
                f"archive skipped: run_id={archive.run_id} error={archive.error}",
                file=sys.stderr,
            )

    summary = _summary(result, archive)
    if args.report_dir is not None:
        report_path = write_daily_report([result], args.report_dir)
        summary["report_file"] = str(report_path)

    text = json.dumps(summary, ensure_ascii=False, indent=2, default=str)
    print(text)

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text, encoding="utf-8")
    if args.html_out is not None:
        args.html_out.mkdir(parents=True, exist_ok=True)
        target = args.html_out / f"{result.run_id}.html"
        target.write_text(_render_html(result, summary), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    # 独立进程没有 FastAPI lifespan：必须在构造 Settings/NodeApiClient 之前
    # 设置 APP_ENV（production → .env.production），否则取数/归档会全空。
    ensure_app_env()
    try:
        trading_date = date_type.fromisoformat(args.date)
    except ValueError:
        print(f"非法 --date: {args.date!r}，需要 YYYY-MM-DD", file=sys.stderr)
        return 2

    # 单事件循环：run_live → archive_run → write_daily_report 共享同一循环，
    # 避免连接池绑定循环关闭后归档静默失败。输出/退出码语义保持不变。
    return asyncio.run(_run_all(args, trading_date))


if __name__ == "__main__":
    raise SystemExit(main())
