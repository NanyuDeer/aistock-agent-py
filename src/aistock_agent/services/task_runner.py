"""定时任务统一执行外壳 — 让 30 个调度任务都有完成态、重试与执行记录（2026-11-18，A2）。

为什么需要外壳
--------------
改造前（``services/scheduler.py``）：每个任务是一个裸协程，内部直接
``await morning_agent.run(state)``。四个后果：

1. **无完成态**：进程中途被杀，没人知道那次任务是"没开始"还是"跑到一半"；
2. **无重试**：传输层抖一下就整次失败，只能等明天；
3. **无执行历史**：出问题只能翻 pm2 日志，跨重启即丢；
4. **无统一降级口径**：每个任务自己 try/except。

本模块只做一件窄事：把「跑一个 agent / 跑一段取数」包一层，统一拿到上面四样。
不统一各 agent 的业务语义 —— 那是 agent 自己（与 registry 的 ``writes``）的事。

为什么**不**把 agent 套进图（重要，别改回去）
--------------------------------------------
初版实现是给每个 agent 编译一张单节点图（START → agent → END），想白拿检查点与
``RetryPolicy``。实测否掉了这个方案，两条硬理由：

1. **图会静默丢弃 state 未声明的返回键。**
   ``AgentState`` 只声明了 messages/intent/final_response 等固定字段，而各 worker
   实际返回大量未声明的键：morning 的 ``cached`` / ``morning_persisted`` /
   ``podcast_brief``，broadcast 的 ``audio_path``，event 的 ``event_id`` /
   ``event_complete``，alert 的 ``report_id`` …… 这些键被 scheduler 与下游直接读取。
   实测（langgraph 1.2.22）：节点返回 ``{"a","cached","audio_path"}``，state 只声明
   ``a`` 时，``stream_mode="updates"`` 与终态里都**只剩 a**，无任何告警。
   即：套图 = 静默改变 worker 的出参 → 下游读到空值 → 产出物缺字段。

2. **单节点图本来也拿不到"中途续跑"。**
   agent 的 ``run`` 是一个整体函数，LangGraph 只在节点边界落检查点。跑一半崩了，
   恢复时还是从 ``run`` 开头重来。所以套图换到的只有「多一次状态序列化 + 检查点
   写入」，换不到续跑。

于是本层改为**直调 + 外壳**：`spec.run(state)` 原样返回什么就交出什么，零失真；
重试与记录由外壳自己保证。若将来确实需要 agent 进图，前提是先把
``AgentState`` 补齐为可容纳各 worker 出参的形状 —— 那是另一件事。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Final

import structlog

from aistock_agent.agents.registry import get_agent
from aistock_agent.graph.retry import is_transient_error
from aistock_agent.services import task_store
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

TRIGGER_SCHEDULER: Final = "scheduler"
TRIGGER_MANUAL: Final = "manual"
TRIGGER_API: Final = "api"

# 外壳自带的重试退避（秒），仅用于 spec.retryable 的 agent 且异常属于传输层。
# 与 graph/retry.py 的 RetryPolicy 同源判定（is_transient_error），只是这里
# 不在图里，得自己退避。
_RETRY_BACKOFF: Final = (1.0, 2.0)
_MAX_ATTEMPTS: Final = 3

_store_ready = False
_store_lock = asyncio.Lock()


@dataclass
class TaskRunResult:
    """一次任务执行的结果（供调用方判断要不要补跑/告警）。"""

    run_id: str
    task_id: str
    status: str
    error: str | None = None
    result: dict[str, Any] | None = None


async def _ensure_store() -> None:
    """建表一次（进程内）。失败只告警：任务不该因记录层不可用而跑不起来。"""
    global _store_ready
    if _store_ready:
        return
    async with _store_lock:
        if _store_ready:
            return
        try:
            await task_store.init_store()
            _store_ready = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("task_runner.init_store_failed", err=str(exc))


def make_agent_state(
    *,
    intent: str,
    session_id: str,
    report_date: str | None = None,
    **extra: Any,
) -> AgentState:
    """构造定时任务用的 AgentState（字段与 scheduler 既有构造保持一致）。

    ``trigger_source="scheduler"`` 是**硬约定**：各 worker 用它守卫落库/缓存等
    副作用（用户对话路径传 "user_chat" 以抑制）。漏设会让定时任务不落库。
    """
    state: AgentState = {
        "messages": [],
        "session_id": session_id,
        "user_id": None,
        "favorites": [],
        "intent": intent,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "report_date": report_date or shanghai_today().isoformat(),
        "trigger_source": TRIGGER_SCHEDULER,
        "final_response": None,
    }
    state.update(extra)  # type: ignore[typeddict-item]
    return state


async def _call_with_retry(
    run: Callable[[AgentState], Awaitable[dict[str, object]]],
    state: AgentState,
    *,
    retryable: bool,
    task_id: str,
) -> dict[str, Any]:
    """直调 agent；retryable 且异常属于传输层时按退避重试（最多 3 次）。"""
    attempts = _MAX_ATTEMPTS if retryable else 1
    last_exc: Exception | None = None
    for attempt in range(attempts):
        try:
            return await run(state)
        except Exception as exc:
            last_exc = exc
            if attempt == attempts - 1 or not is_transient_error(exc):
                raise
            backoff = _RETRY_BACKOFF[min(attempt, len(_RETRY_BACKOFF) - 1)]
            logger.warning(
                "task_runner.retry",
                task_id=task_id,
                attempt=attempt + 1,
                backoff_s=backoff,
                err=str(exc),
            )
            await asyncio.sleep(backoff)
    assert last_exc is not None  # 循环必在末次抛出，此处不可达
    raise last_exc


async def run_agent_task(
    task_id: str,
    agent_name: str,
    state: AgentState,
    *,
    trigger: str = TRIGGER_SCHEDULER,
) -> TaskRunResult:
    """跑一个 agent 任务（直调 + 执行记录 + 传输层重试）。

    返回的 ``result`` 是 agent ``run`` 的**原始返回**（可能含 AgentState 未声明的
    键），与改造前 scheduler 直调拿到的对象等价。
    """
    spec = get_agent(agent_name)
    report_date = str(state.get("report_date") or shanghai_today().isoformat())
    thread_id = f"task:{task_id}:{report_date}"

    await _ensure_store()
    run_id = await task_store.record_start(
        task_id=task_id, agent=agent_name, thread_id=thread_id, trigger=trigger
    )
    logger.info("task_runner.start", task_id=task_id, agent=agent_name, run_id=run_id)

    try:
        out = await _call_with_retry(
            spec.run, state, retryable=spec.retryable, task_id=task_id
        )
    except Exception as exc:
        # 原样抛出：APScheduler 需要看到异常才会把 job 标成 error 并按 misfire 处理；
        # 静默吞掉等于把失败伪装成成功（改造前正是这个毛病）。
        await task_store.record_finish(
            run_id, status=task_store.STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"
        )
        logger.error(
            "task_runner.failed", task_id=task_id, agent=agent_name, err=str(exc), exc_info=True
        )
        raise

    payload = out if isinstance(out, dict) else {}
    # 空产物 = 跑完了但什么都没产（对齐 escalate 对「空 final_response」的降级判定）
    status = task_store.STATUS_OK if payload else task_store.STATUS_DEGRADED
    await task_store.record_finish(
        run_id, status=status, result_keys=list(payload.keys())
    )
    logger.info("task_runner.done", task_id=task_id, status=status, run_id=run_id)
    return TaskRunResult(run_id=run_id, task_id=task_id, status=status, result=payload)


async def run_data_task(
    task_id: str,
    body: Callable[[], Awaitable[Any]],
    *,
    trigger: str = TRIGGER_SCHEDULER,
) -> TaskRunResult:
    """跑一个数据/计算类任务（无 agent，但同样留执行记录）。

    刻意不套图：sentiment_temp / intraday_snapshot / calendar 这些没有 agent 语义，
    套图只会多一次状态序列化与检查点写入。
    """
    await _ensure_store()
    run_id = await task_store.record_start(task_id=task_id, trigger=trigger)
    try:
        # 数据任务没有统一出参约定，返回值不参与记录（各任务返回形状各异）
        await body()
    except Exception as exc:
        await task_store.record_finish(
            run_id, status=task_store.STATUS_FAILED, error=f"{type(exc).__name__}: {exc}"
        )
        logger.error("task_runner.data_failed", task_id=task_id, err=str(exc), exc_info=True)
        raise
    await task_store.record_finish(run_id, status=task_store.STATUS_OK)
    logger.info("task_runner.data_done", task_id=task_id, run_id=run_id)
    return TaskRunResult(run_id=run_id, task_id=task_id, status=task_store.STATUS_OK)


def reset_store_ready_for_tests() -> None:  # pragma: no cover - 测试隔离用
    """重置建表标志（测试切换 task_runs_path 后需要）。"""
    global _store_ready
    _store_ready = False
