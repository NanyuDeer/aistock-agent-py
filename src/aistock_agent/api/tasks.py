"""定时任务执行记录的只读查询接口（2026-11-18，框架收敛 A4）。

为什么单独成模块
----------------
``api/routes.py`` 已 2290 行。执行记录查询是**运维面**（排查"昨天节奏大师为什么
没跑"），与业务面（chat / briefing / report）关注点不同，混进同一个文件只会让
它继续膨胀。

鉴权与同仓 ``/internal/*`` 一致：``X-Internal-Token``（内部 token），只对内网开放。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query

from aistock_agent.api.deps import verify_internal_token
from aistock_agent.services import task_store

router = APIRouter(tags=["internal-tasks"])


@router.get(
    "/internal/tasks/runs",
    dependencies=[Depends(verify_internal_token)],
)
async def list_task_runs(
    task_id: str | None = Query(None, description="调度 job id，如 rhythm_master_after_close"),
    date: str | None = Query(None, description="启动日期 YYYY-MM-DD（按 Asia/Shanghai）"),
    limit: int = Query(50, ge=1, le=500),
) -> dict[str, Any]:
    """按任务/日期倒序查询执行记录。

    典型用法：
    - ``?date=2026-11-18`` —— 今天所有任务跑了什么
    - ``?task_id=rhythm_master_after_close&date=2026-11-17`` —— 那一次到底怎么了

    状态语义：``running`` 表示"开始了但没写完成态"（进程被杀/仍在跑），
    ``degraded`` 是跑完但无产物，``failed`` 才带 ``error`` 文本。
    """
    runs = await task_store.list_runs(task_id=task_id, date=date, limit=limit)
    return {"success": True, "count": len(runs), "runs": runs}


@router.get(
    "/internal/tasks/runs/{run_id}",
    dependencies=[Depends(verify_internal_token)],
)
async def get_task_run(run_id: str) -> dict[str, Any]:
    """单次执行详情（含 error 全文，用于定位失败原因）。"""
    row = await task_store.get_run(run_id)
    if row is None:
        return {"success": False, "message": "run not found", "run": None}
    return {"success": True, "run": row}
