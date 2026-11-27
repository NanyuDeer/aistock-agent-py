"""P7：CLI 全流程跑在同一个事件循环内（全部离线）。

背景：旧 `main()` 用两次 `asyncio.run(...)`（`run_live` 一次、`archive_run`
一次）。`HttpClientPool` 的 httpx.AsyncClient 绑定第一个事件循环，循环关闭后
归档请求静默失败（`archive.error="empty_response"`）。本文件用 monkeypatch
替换 `run_live` / `archive_run` / `write_daily_report`，记录各自所在的运行
事件循环对象，断言三者共享同一事件循环且顺序正确。

不触网、不真实取数/归档、不写真实报告。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aistock_agent.brain_v4.runtime import cli
from aistock_agent.brain_v4.runtime.archive import ArchiveResult
from aistock_agent.brain_v4.runtime.runner import LiveRunResult

CST = UTC


def _result() -> LiveRunResult:
    as_of = datetime(2026, 11, 27, 15, 35, tzinfo=CST)
    data_cutoff = datetime(2026, 11, 27, 15, 0, tzinfo=CST)
    return LiveRunResult(
        run_id="run_p7_cli",
        profile="market_trace",
        as_of=as_of,
        data_cutoff=data_cutoff,
        snapshot_id="snap_p7_cli",
        snapshot_sha="sha_p7_cli",
        status="ok",
    )


def test_run_live_archive_and_report_share_single_event_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    loops: dict[str, asyncio.AbstractEventLoop] = {}
    order: list[str] = []

    async def fake_run_live(profile: str, date: object, **kwargs: object) -> LiveRunResult:
        loops["run_live"] = asyncio.get_running_loop()
        order.append("run_live")
        return _result()

    async def fake_archive_run(
        result: LiveRunResult, **kwargs: object
    ) -> ArchiveResult:
        loops["archive_run"] = asyncio.get_running_loop()
        order.append("archive_run")
        return ArchiveResult(archived=True, run_id=result.run_id)

    def fake_write_daily_report(
        results: object, out_dir: object
    ) -> Path:
        loops["write_daily_report"] = asyncio.get_running_loop()
        order.append("write_daily_report")
        return Path(out_dir) / "2026-11-27" / "index.html"

    monkeypatch.setattr(cli, "run_live", fake_run_live)
    monkeypatch.setattr(cli, "archive_run", fake_archive_run)
    monkeypatch.setattr(cli, "write_daily_report", fake_write_daily_report)

    exit_code = cli.main(
        [
            "--profile",
            "market_trace",
            "--date",
            "2026-11-27",
            "--archive",
            "--report-dir",
            str(tmp_path),
        ]
    )

    assert exit_code == 0
    assert order == ["run_live", "archive_run", "write_daily_report"]
    # 同一事件循环对象（持有引用，避免不同循环因地址复用产生假相等）。
    assert loops["run_live"] is loops["archive_run"]
    assert loops["archive_run"] is loops["write_daily_report"]
