"""定时任务执行记录 — 让"昨天那个任务为什么没跑"有答案（2026-11-18，A1）。

改造前的事实
------------
30 个 APScheduler 任务全是 ``scheduler.py`` 里直调 ``agent.run(state)`` 的裸协程：
没有统一外壳、没有完成态、失败只在 pm2 日志里留一行。要回答
「节奏大师收盘那次跑了吗 / 跑成什么样」只能翻日志，且跨进程/重启后无迹可寻。

本模块提供的是**记录**，不是编排：
- 每次任务执行一行 ``task_runs``；
- 状态机 ``running → ok | failed | degraded | skipped``；
- 按 task_id / 日期可查（API 见 ``api/routes.py`` 的 /internal/tasks/runs）。

设计取舍
--------
1. **独立 sqlite 文件**（``settings.task_runs_path``），不复用 ``.langgraph.db``：
   检查点由 LangGraph 内部管理、会被压缩/清理，执行历史是业务资产，两者生命周期不同。
2. **每操作独立连接**：任务频率是"每天几十次"，连接开销可忽略；换来的是不用管
   长连接在事件循环/线程之间迁移的问题（``memory/checkpointer.py`` 为长连接付过
   这个代价，有 atexit 与 GeneratorExit 两处补丁）。
3. **写入失败只告警不抛**：记录是观测量，不是控制流。若因磁盘满/库锁导致
   ``record_start`` 失败，任务本身不该因此失败。读取（``list_runs``）相反 ——
   它是显式查询，失败必须让调用方知道。
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Final

import aiosqlite
import structlog

from aistock_agent.config import settings
from aistock_agent.utils.date import shanghai_now

logger = structlog.get_logger()

# 状态取值。degraded = 跑完了但走了降级路径（产出物不完整），与 failed（抛异常）区分：
# 两者对"要不要补跑"的决策完全不同。
STATUS_RUNNING: Final = "running"
STATUS_OK: Final = "ok"
STATUS_FAILED: Final = "failed"
STATUS_DEGRADED: Final = "degraded"
STATUS_SKIPPED: Final = "skipped"

_SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS task_runs (
    run_id      TEXT PRIMARY KEY,
    task_id     TEXT NOT NULL,
    agent       TEXT,
    thread_id   TEXT,
    status      TEXT NOT NULL,
    trigger     TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    ended_at    TEXT,
    duration_ms INTEGER,
    error       TEXT,
    result_keys TEXT
);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_started
    ON task_runs(task_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_task_runs_started
    ON task_runs(started_at DESC);
"""


async def _connect() -> aiosqlite.Connection:
    conn = await aiosqlite.connect(
        settings.task_runs_path, timeout=settings.sqlite_busy_timeout
    )
    # WAL：查询（/internal/tasks/runs）不阻塞写入；NORMAL 同步级别足够（记录可容忍
    # 进程崩溃丢最后一条，但不能接受 fsync 拖慢调度）。
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    return conn


async def init_store() -> None:
    """建表（幂等）。由 ``services/task_runner.py`` 首次使用时调用。"""
    conn = await _connect()
    try:
        await conn.executescript(_SCHEMA)
        await conn.commit()
    finally:
        await conn.close()


async def record_start(
    *,
    task_id: str,
    agent: str | None = None,
    thread_id: str | None = None,
    trigger: str = "scheduler",
    run_id: str | None = None,
) -> str:
    """写入一条 running 记录，返回 run_id（供后续 record_finish 关联）。"""
    rid = run_id or uuid.uuid4().hex
    started = shanghai_now().isoformat()
    try:
        conn = await _connect()
        try:
            await conn.execute(
                "INSERT INTO task_runs "
                "(run_id, task_id, agent, thread_id, status, trigger, started_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (rid, task_id, agent, thread_id, STATUS_RUNNING, trigger, started),
            )
            await conn.commit()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001 — 记录失败不阻断任务（见模块 docstring）
        logger.warning("task_store.record_start_failed", task_id=task_id, err=str(exc))
    return rid


async def record_finish(
    run_id: str,
    *,
    status: str,
    error: str | None = None,
    result_keys: list[str] | None = None,
) -> None:
    """收尾：写 ended_at / duration_ms / status / error。"""
    ended_dt = shanghai_now()
    try:
        conn = await _connect()
        try:
            # duration 用 sqlite 侧 julianday 计算，避免读写两次时钟不同步；
            # 精度到毫秒足够定位"跑得慢"还是"没跑"。
            await conn.execute(
                "UPDATE task_runs SET status = ?, ended_at = ?, "
                "duration_ms = CAST((julianday(?) - julianday(started_at)) * 86400000 AS INTEGER), "
                "error = ?, result_keys = ? WHERE run_id = ?",
                (
                    status,
                    ended_dt.isoformat(),
                    ended_dt.isoformat(),
                    error,
                    json.dumps(result_keys, ensure_ascii=False) if result_keys else None,
                    run_id,
                ),
            )
            await conn.commit()
        finally:
            await conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("task_store.record_finish_failed", run_id=run_id, err=str(exc))


async def list_runs(
    *,
    task_id: str | None = None,
    date: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """按 task_id / 日期（YYYY-MM-DD，按 started_at 前 10 位）倒序查询。

    这是显式查询路径：库不可用时**抛出**，不静默返回空列表（否则前端会以为
    "当天没跑"，与真实故障无法区分）。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if date:
        clauses.append("substr(started_at, 1, 10) = ?")
        params.append(date)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    params.append(max(1, min(limit, 500)))

    conn = await _connect()
    try:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT run_id, task_id, agent, thread_id, status, trigger, started_at, "
            f"ended_at, duration_ms, error, result_keys FROM task_runs {where} "
            "ORDER BY started_at DESC LIMIT ?",
            params,
        ) as cur:
            rows = await cur.fetchall()
    finally:
        await conn.close()

    out: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        raw_keys = item.get("result_keys")
        item["result_keys"] = json.loads(raw_keys) if raw_keys else []
        out.append(item)
    return out


async def get_run(run_id: str) -> dict[str, Any] | None:
    """按 run_id 取单条（失败排查用，返回 error 全文）。"""
    conn = await _connect()
    try:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT * FROM task_runs WHERE run_id = ?", (run_id,)
        ) as cur:
            row = await cur.fetchone()
    finally:
        await conn.close()
    if row is None:
        return None
    item = dict(row)
    # 与 list_runs 保持同一出参形状：result_keys 一律是 list，不要一半原始 JSON 串
    raw_keys = item.get("result_keys")
    item["result_keys"] = json.loads(raw_keys) if raw_keys else []
    return item
