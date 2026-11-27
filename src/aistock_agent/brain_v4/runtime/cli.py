"""V4 实时运行 CLI：手工触发一次真实取数 run 并输出摘要。

用法：
    python -m aistock_agent.brain_v4.runtime.cli \
        --profile market_trace --date 2026-11-27 [--json-out PATH] [--html-out DIR]

为什么保留 CLI：P3 需要人工核验真实数据链路；P4 调度器之后复用同一 `run_live`，
CLI 只做参数解析与摘要输出，不复制任何取数/研判逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
from datetime import date as date_type
from pathlib import Path

from aistock_agent.brain_v4.runtime.runner import LiveRunResult, run_live

_PROFILES = (
    "market_trace",
    "briefing_morning",
    "briefing_midday",
    "briefing_evening",
)


def _summary(result: LiveRunResult) -> dict[str, object]:
    judgment = result.judgment
    six_factor = result.six_factor
    return {
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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        trading_date = date_type.fromisoformat(args.date)
    except ValueError:
        print(f"非法 --date: {args.date!r}，需要 YYYY-MM-DD", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(run_live(args.profile, trading_date))
    except Exception as exc:  # noqa: BLE001 - CLI 边界统一转非零退出码
        print(f"V4 live run 失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = _summary(result)
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


if __name__ == "__main__":
    raise SystemExit(main())
