"""ChatAgent 后台生成任务管理器（问题 15 断点续传，2026-08-11）。

生成任务与 WS 连接解耦：任务在后台执行，事件记录在 state.events 供 resume
回放，终态结果（DONE/ERROR payload）缓存供回页补全。同 session 并发双跑
由 start() 拒绝。单事件循环内使用，无需锁。
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_RESULT_TTL_SEC = 600  # 完成后结果保留 10 分钟

# producer 总时长兜底（LLM 单次 600s + 60s 余量，对齐 llm.py:62 的 _LLM_REQUEST_TIMEOUT_SECONDS）
_RUN_TOTAL_TIMEOUT_SEC = 660

_CONFIRM_TTL_SEC = 600  # pending confirm 保留 10 分钟（对齐 result TTL；确认窗口 60s 远小于 TTL）

# 事件 sink：接收一个 WS 就绪 payload dict
EventSink = Callable[[dict], Awaitable[None]]


@dataclass
class ChatRunState:
    session_id: str
    run_id: str
    task: asyncio.Task
    user_id: str | None = None  # 归属（P0 服务端注入值，未登录 None；resume/stop 越权校验用）
    events: list[dict] = field(default_factory=list)
    waiters: set[asyncio.Event] = field(default_factory=set)
    done: bool = False
    finalizing: bool = False  # producer 已产出终态 result，进入收尾（cancel 拒绝窗口）
    cancelled: bool = False  # cancelled 终态标记（done 后为 True 表示被用户停止）
    result: dict | None = None
    created_at: float = field(default_factory=time.monotonic)
    done_at: float | None = None

    def notify(self) -> None:
        """唤醒全部等待新事件的转发协程。"""
        for w in list(self.waiters):
            w.set()

    def cancel(self) -> None:
        """停止当前 run：置 cancelled 标记 + 取消后台任务。
        done 的 task 调 cancel() 是 no-op，安全。"""
        self.cancelled = True
        if not self.task.done():
            self.task.cancel()


class ChatTaskManager:
    """session_id → ChatRunState 的后台任务管理器（模块级单例）。"""

    _states: dict[str, ChatRunState] = {}
    # Phase 4 验收修复（B2/C2）：pending-confirm 独立缓存，keyed by session_id，
    # 存活于 ChatRunState 之外——阶段 2 start() 会覆盖 _states[session_id]，
    # 若只放 state 上会被新 run 冲掉；独立缓存才能支撑 resume 后补发/消费与幂等。
    _pending_confirm: dict[str, dict] = {}

    def set_pending_confirm(self, session_id: str, payload: dict) -> None:
        payload["created_at"] = time.monotonic()
        self._pending_confirm[session_id] = payload

    def get_pending_confirm(self, session_id: str) -> dict | None:
        p = self._pending_confirm.get(session_id)
        if p is None:
            return None
        if time.monotonic() - p.get("created_at", 0.0) > _CONFIRM_TTL_SEC:
            self._pending_confirm.pop(session_id, None)
            return None
        return p

    def clear_pending_confirm(self, session_id: str) -> None:
        self._pending_confirm.pop(session_id, None)

    def start(
        self,
        session_id: str,
        run_id: str,
        producer: Callable[[ChatRunState], Awaitable[dict | None]],
        user_id: str | None = None,
    ) -> ChatRunState | None:
        """启动后台生成任务。

        producer(state): 后台协程，负责执行 graph 并把 WS 事件 append 进
        state.events + state.notify()；结束返回终态 payload（DONE/ERROR dict）。
        user_id: P0 服务端注入的归属（未登录 None），供 resume/stop 越权校验。
        返回 None 表示同 session 已有活跃任务（并发拒绝）。
        """
        self._cleanup()
        existing = self._states.get(session_id)
        if existing and not existing.done:
            return None

        async def _runner() -> None:
            started = time.monotonic()
            try:
                # 总时长兜底：asyncio.timeout 在 _runner 内联执行 producer（无独立
                # 内层 task → 调度与 BASE 一致、无任务泄漏）；超时抛内置
                # TimeoutError（Py3.11 起与 asyncio.TimeoutError 同义），由下方分支处理。
                async with asyncio.timeout(_RUN_TOTAL_TIMEOUT_SEC):
                    state.result = await producer(state)
                # 收尾窗口：result 已产出，置 finalizing 拒绝窗口内 cancel（防误杀将成之轮）
                state.finalizing = True
            except asyncio.CancelledError:
                # 用户停止（stop → task.cancel()）：CancelledError 继承 BaseException，
                # 不被 except Exception 捕获，必须显式处理并置 cancelled 终态（spec §8.2）
                state.result = {"type": "cancelled", "content": "已停止生成"}
            except TimeoutError:
                # 总时长兜底（T2）：producer 卡死时置 ERROR 终态，session 释放可重试。
                # TimeoutError 继承 Exception，必须在此显式处理，避免落入 producer_failed 死区。
                logger.warning(
                    "chat.run_timeout session_id=%s elapsed_ms=%d",
                    session_id, int((time.monotonic() - started) * 1000),
                )
                state.result = {"type": "error", "content": "生成超时，请稍后重试"}
            except Exception:
                logger.exception(
                    "chat_task_manager.producer_failed session_id=%s", session_id
                )
            finally:
                state.done = True
                state.notify()
                # done_at 仅由本管理器写一次：producer 若已设置（测试强制过期场景）
                # 则保留，避免覆盖导致 TTL 用例失效
                if state.done_at is None:
                    state.done_at = time.monotonic()
                # 观测种子（治理集 G3）：每轮 start→终态耗时 + 是否正常完成
                logger.info(
                    "chat.run_finished session_id=%s elapsed_ms=%d done=%s",
                    session_id, int((time.monotonic() - started) * 1000),
                    state.result is not None and state.result.get("type") in ("done", "cancelled"),
                )

        state = ChatRunState(
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
            task=asyncio.create_task(_runner()),
        )
        self._states[session_id] = state
        return state

    def get(self, session_id: str) -> ChatRunState | None:
        """取状态；done 且超过 TTL 惰性删除并返回 None。"""
        s = self._states.get(session_id)
        if s is None:
            return None
        if s.done and s.done_at is not None and time.monotonic() - s.done_at > _RESULT_TTL_SEC:
            self._states.pop(session_id, None)
            return None
        return s

    def has_active(self, session_id: str) -> bool:
        s = self._states.get(session_id)
        return bool(s and not s.done)

    def cancel(self, session_id: str) -> bool:
        """停止 session 的活跃 run；无活跃 run 返回 False（stop_status not_found 依据）。"""
        s = self._states.get(session_id)
        if s is None or s.done or s.finalizing:
            return False
        s.cancel()
        return True

    def _cleanup(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, s in self._states.items()
            if s.done and s.done_at is not None and now - s.done_at > _RESULT_TTL_SEC
        ]
        for sid in expired:
            self._states.pop(sid, None)
        now = time.monotonic()
        expired_confirm = [
            sid for sid, p in self._pending_confirm.items()
            if now - p.get("created_at", 0.0) > _CONFIRM_TTL_SEC
        ]
        for sid in expired_confirm:
            self._pending_confirm.pop(sid, None)

    async def _cleanup_for_test(self) -> None:
        """测试辅助：清空全部状态。"""
        self._states.clear()
        self._pending_confirm.clear()


chat_task_manager = ChatTaskManager()
