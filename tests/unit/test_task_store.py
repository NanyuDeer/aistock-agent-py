"""任务执行记录（task_store）测试 — 2026-11-18，框架收敛 A1。

覆盖：建表幂等、写 running/收尾 ok、失败路径写 error、按 task/日期查询、
写入失败不抛（记录是观测量）、查询失败要抛（显式查询不能静默空）。
"""
from __future__ import annotations

import aiosqlite
import pytest

from aistock_agent.config import settings
from aistock_agent.services import task_store
from aistock_agent.utils.date import shanghai_today


@pytest.fixture()
def store(tmp_path, monkeypatch):
    db = tmp_path / "task_runs.db"
    monkeypatch.setattr(settings, "task_runs_path", str(db), raising=False)
    return db


@pytest.mark.asyncio
async def test_init_is_idempotent(store) -> None:
    await task_store.init_store()
    await task_store.init_store()
    assert store.exists()


@pytest.mark.asyncio
async def test_roundtrip_ok(store) -> None:
    await task_store.init_store()
    run_id = await task_store.record_start(
        task_id="rhythm_master_after_close",
        agent="rhythm_master",
        thread_id="task:rhythm_master:after_close",
    )
    await task_store.record_finish(run_id, status=task_store.STATUS_OK, result_keys=["cards"])

    rows = await task_store.list_runs(task_id="rhythm_master_after_close")
    assert len(rows) == 1
    row = rows[0]
    assert row["status"] == "ok"
    assert row["agent"] == "rhythm_master"
    assert row["trigger"] == "scheduler"
    assert row["result_keys"] == ["cards"]
    assert row["ended_at"] is not None
    assert row["duration_ms"] is not None


@pytest.mark.asyncio
async def test_failure_records_error_text(store) -> None:
    """"昨天为什么没跑"的核心：error 全文必须能查出来。"""
    await task_store.init_store()
    run_id = await task_store.record_start(task_id="review_full", agent="review")
    await task_store.record_finish(
        run_id, status=task_store.STATUS_FAILED, error="TimeoutError: upstream 600s"
    )

    row = await task_store.get_run(run_id)
    assert row is not None
    assert row["status"] == "failed"
    assert "TimeoutError" in row["error"]


@pytest.mark.asyncio
async def test_running_row_survives_without_finish(store) -> None:
    """进程中途崩掉 → 记录停在 running，正是"跑了但没跑完"的可观测信号。"""
    await task_store.init_store()
    await task_store.record_start(task_id="morning_briefing", agent="morning")

    rows = await task_store.list_runs(task_id="morning_briefing")
    assert rows[0]["status"] == "running"
    assert rows[0]["ended_at"] is None


@pytest.mark.asyncio
async def test_list_filters_by_date(store) -> None:
    await task_store.init_store()
    rid = await task_store.record_start(task_id="midday_briefing")
    await task_store.record_finish(rid, status=task_store.STATUS_OK)

    today = shanghai_today().isoformat()
    assert len(await task_store.list_runs(date=today)) == 1
    assert await task_store.list_runs(date="1999-01-01") == []


@pytest.mark.asyncio
async def test_record_start_swallows_storage_error(tmp_path, monkeypatch) -> None:
    """记录写失败不能把任务搞挂：只告警，仍返回 run_id。"""
    bad = tmp_path / "not_a_db" / "x.db"  # 父目录不存在 → sqlite 打不开
    monkeypatch.setattr(settings, "task_runs_path", str(bad), raising=False)

    run_id = await task_store.record_start(task_id="morning_briefing")
    assert isinstance(run_id, str) and run_id


@pytest.mark.asyncio
async def test_list_runs_raises_when_store_unavailable(tmp_path, monkeypatch) -> None:
    """显式查询失败必须抛，不能返回空列表冒充"当天没跑"。"""
    bad = tmp_path / "not_a_db" / "x.db"
    monkeypatch.setattr(settings, "task_runs_path", str(bad), raising=False)

    with pytest.raises((aiosqlite.Error, OSError)):
        await task_store.list_runs(task_id="morning_briefing")
