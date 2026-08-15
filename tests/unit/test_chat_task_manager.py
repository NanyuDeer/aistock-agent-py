"""ChatTaskManager 单测（问题 15：生成任务与 WS 连接解耦）"""
import asyncio
import time

import pytest

from aistock_agent.services.chat_task_manager import chat_task_manager


@pytest.mark.asyncio
async def test_start_and_done_result_cached():
    """任务跑完后 result 缓存，get 返回 done 状态。"""
    manager = chat_task_manager

    async def producer(state):
        state.events.append({"type": "text", "content": "hi"})
        state.notify()
        await asyncio.sleep(0)  # 给 waiters 一次轮询机会
        return {"type": "done", "content": "最终回答"}

    state = manager.start("s1", "r1", producer)
    assert state is not None
    await state.task
    assert state.done is True
    assert state.result == {"type": "done", "content": "最终回答"}
    got = manager.get("s1")
    assert got is state


@pytest.mark.asyncio
async def test_start_rejects_concurrent_active_run():
    """同 session 活跃任务存在时 start 返回 None（防并发双跑）。"""
    manager = chat_task_manager

    async def slow_producer(state):
        await asyncio.sleep(0.5)
        return {"type": "done"}

    first = manager.start("s2", "r1", slow_producer)
    assert first is not None
    second = manager.start("s2", "r2", slow_producer)
    assert second is None
    await first.task
    # done 之后可再次 start
    third = manager.start("s2", "r3", slow_producer)
    assert third is not None
    await third.task


@pytest.mark.asyncio
async def test_get_returns_none_after_ttl():
    """done 超 TTL 后 get 返回 None（惰性清理）。"""
    manager = chat_task_manager
    await manager._cleanup_for_test()  # 清空单例，避免跨测试污染
    async def producer(state):
        state.done_at = time.monotonic() - 601  # 强制过期
        return {"type": "done"}

    state = manager.start("s3", "r1", producer)
    assert state is not None
    await state.task
    assert manager.get("s3") is None


@pytest.mark.asyncio
async def test_events_replay_and_waiter_notify():
    """events 累积 + notify 唤醒等待者（resume 回放基础）。"""
    manager = chat_task_manager
    seen: list[dict] = []

    async def collect(state):
        waiter = asyncio.Event()
        state.waiters.add(waiter)
        try:
            await waiter.wait()
        finally:
            state.waiters.discard(waiter)
        for ev in state.events:
            seen.append(ev)

    async def producer(state):
        state.events.append({"type": "text", "content": "a"})
        state.notify()
        await asyncio.sleep(0.01)
        state.events.append({"type": "text", "content": "b"})
        state.notify()
        await asyncio.sleep(0.01)
        return {"type": "done"}

    state = manager.start("s4", "r1", producer)
    assert state is not None
    collector = asyncio.create_task(collect(state))
    await state.task
    await collector
    assert [e["content"] for e in seen] == ["a", "b"]


@pytest.mark.asyncio
async def test_cancel_active_run_yields_cancelled_terminal():
    """cancel 活跃 run → done+cancelled=True，result 为 cancelled 终态 payload。"""
    manager = chat_task_manager
    await manager._cleanup_for_test()
    started = asyncio.Event()

    async def slow_producer(state):
        started.set()
        await asyncio.sleep(30)
        return {"type": "done", "content": "x"}

    state = manager.start("c1", "r1", slow_producer, "o_a")
    assert state is not None
    await started.wait()
    assert manager.cancel("c1") is True
    await state.task
    assert state.done is True
    assert state.cancelled is True
    assert state.result == {"type": "cancelled", "content": "已停止生成"}
    # done 后可再次 start（has_active 释放）
    again = manager.start("c1", "r2", slow_producer, "o_a")
    assert again is not None
    # Py3.11：create_task 的协程未被事件循环驱动时 cancel() 直接把
    # CancelledError 当作任务结果（协程体不执行，_runner 捕不到），
    # 先 sleep(0) 让任务启动挂起（producer 在 sleep(30)），取消才能被正常捕获。
    await asyncio.sleep(0)
    again.cancel()
    await again.task


@pytest.mark.asyncio
async def test_cancel_no_active_run_returns_false():
    """无活跃 run → cancel 返回 False（stop_status not_found 依据）。"""
    manager = chat_task_manager
    await manager._cleanup_for_test()
    assert manager.cancel("missing") is False


@pytest.mark.asyncio
async def test_start_records_user_id_for_ownership():
    """start 记录 user_id 归属（resume/stop 越权校验的数据源）。"""
    manager = chat_task_manager
    await manager._cleanup_for_test()

    async def producer(state):
        return {"type": "done"}

    state = manager.start("c3", "r1", producer, "o_owner")
    assert state is not None
    assert state.user_id == "o_owner"
    await state.task
    assert manager.get("c3") is not None
    await manager._cleanup_for_test()


# ── Phase 4 验收修复（B2/C2）：pending-confirm 独立缓存 ─────────────────────


@pytest.mark.asyncio
async def test_pending_confirm_set_get_clear():
    ctm = chat_task_manager
    ctm.clear_pending_confirm("s1")
    assert ctm.get_pending_confirm("s1") is None
    ctm.set_pending_confirm(
        "s1",
        {"request_id": "r1", "question": "q", "options": [], "run_id": "r1", "user_id": None},
    )
    got = ctm.get_pending_confirm("s1")
    assert got is not None and got["request_id"] == "r1"
    ctm.clear_pending_confirm("s1")
    assert ctm.get_pending_confirm("s1") is None


@pytest.mark.asyncio
async def test_pending_confirm_expires_after_ttl(monkeypatch):
    import aistock_agent.services.chat_task_manager as m
    ctm = m.ChatTaskManager()
    ctm._pending_confirm = {}
    monkeypatch.setattr(m, "_CONFIRM_TTL_SEC", -1.0)  # 已过期
    ctm.set_pending_confirm(
        "s1",
        {"request_id": "r1", "question": "q", "options": [], "run_id": "r1", "user_id": None},
    )
    assert ctm.get_pending_confirm("s1") is None


# ── 问题 20 B：finalizing 护栏（cancel 不误杀将成之轮） ────────────────────


@pytest.mark.asyncio
async def test_cancel_rejected_when_finalizing():
    """producer 已产出终态 result（finalizing=True）后 cancel 应拒绝（防误杀将成之轮）。"""
    from aistock_agent.services.chat_task_manager import ChatTaskManager

    manager = ChatTaskManager()

    async def producer(state):
        state.result = {"type": "done", "content": "ok"}
        state.finalizing = True  # 复刻 _runner 在 result 赋后置位
        await asyncio.sleep(0.05)  # 保持 finalizing 窗口：result 已产出但 done 未置位
        return state.result

    state = manager.start("s1", "r1", producer)
    assert state is not None
    # 让 producer 进入 finalizing 窗口（result 已产出、done 未置位）
    await asyncio.sleep(0)
    # 窗口内 cancel 必须被拒绝；RED 阶段（无护栏）返回 True → 断言失败
    assert manager.cancel("s1") is False
    await state.task
    # 未误杀：终态保持 done，而非被取消成 cancelled
    assert state.result == {"type": "done", "content": "ok"}
    assert state.cancelled is False
    await manager._cleanup_for_test()


@pytest.mark.asyncio
async def test_cancel_rejected_when_done():
    """done 后 cancel 返回 False（既有语义，回归锁定）。"""
    from aistock_agent.services.chat_task_manager import ChatTaskManager

    manager = ChatTaskManager()

    async def producer(state):
        return {"type": "done", "content": "ok"}

    manager.start("s2", "r1", producer)
    await asyncio.sleep(0)
    assert manager.cancel("s2") is False
    await manager._cleanup_for_test()
