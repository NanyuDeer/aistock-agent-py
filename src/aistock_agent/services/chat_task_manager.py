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

# 事件 sink：接收一个 WS 就绪 payload dict
EventSink = Callable[[dict], Awaitable[None]]


@dataclass
class ChatRunState:
    session_id: str
    run_id: str
    task: asyncio.Task
    events: list[dict] = field(default_factory=list)
    waiters: set[asyncio.Event] = field(default_factory=set)
    done: bool = False
    result: dict | None = None
    created_at: float = field(default_factory=time.monotonic)
    done_at: float | None = None

    def notify(self) -> None:
        """唤醒全部等待新事件的转发协程。"""
        for w in list(self.waiters):
            w.set()


class ChatTaskManager:
    """session_id → ChatRunState 的后台任务管理器（模块级单例）。"""

    _states: dict[str, ChatRunState] = {}

    def start(
        self,
        session_id: str,
        run_id: str,
        producer: Callable[[ChatRunState], Awaitable[dict | None]],
    ) -> ChatRunState | None:
        """启动后台生成任务。

        producer(state): 后台协程，负责执行 graph 并把 WS 事件 append 进
        state.events + state.notify()；结束返回终态 payload（DONE/ERROR dict）。
        返回 None 表示同 session 已有活跃任务（并发拒绝）。
        """
        self._cleanup()
        existing = self._states.get(session_id)
        if existing and not existing.done:
            return None

        async def _runner() -> None:
            try:
                state.result = await producer(state)
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

        state = ChatRunState(
            session_id=session_id,
            run_id=run_id,
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

    def _cleanup(self) -> None:
        now = time.monotonic()
        expired = [
            sid for sid, s in self._states.items()
            if s.done and s.done_at is not None and now - s.done_at > _RESULT_TTL_SEC
        ]
        for sid in expired:
            self._states.pop(sid, None)

    async def _cleanup_for_test(self) -> None:
        """测试辅助：清空全部状态。"""
        self._states.clear()


chat_task_manager = ChatTaskManager()
