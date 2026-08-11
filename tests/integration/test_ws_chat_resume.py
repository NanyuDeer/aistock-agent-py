"""ws resume 集成测试（问题 15 断点续传）。

直接单测 ws_chat 的 resume 分支逻辑（不经真实 LLM）：
- resume 命中 done → 直接补发终态 payload
- resume 命中 running → resume_status running + 回放事件
- resume 无记录 → resume_status none
- 普通消息 + 同 session 活跃任务 → 并发拒绝
"""
import asyncio

import pytest

from aistock_agent.api.ws import _forward
from aistock_agent.services.chat_task_manager import ChatTaskManager


class FakeRunState:
    """最小 ChatRunState 形状（不启动真实 task）。"""

    def __init__(self, events, done, result):
        self.session_id = "s"
        self.run_id = "r1"
        self.events = events
        self.waiters = set()
        self.done = done
        self.result = result
        self.task = None
        self.created_at = 0
        self.done_at = None


@pytest.mark.asyncio
async def test_forward_replays_events_and_sends_terminal():
    """done 状态：replay=True 回放全部 events + 终态 payload。"""
    state = FakeRunState(
        events=[{"type": "text", "content": "a"}, {"type": "text", "content": "b"}],
        done=True,
        result={"type": "done", "content": "终态"},
    )
    sent: list[dict] = []

    async def send(payload: dict) -> None:
        sent.append(payload)

    await _forward(state, send, replay=True)
    assert sent == [
        {"type": "text", "content": "a"},
        {"type": "text", "content": "b"},
        {"type": "done", "content": "终态"},
    ]


@pytest.mark.asyncio
async def test_forward_live_follows_new_events():
    """running 状态：replay=False 只转发新增事件，done 后发终态。"""
    state = FakeRunState(
        events=[{"type": "text", "content": "a"}],
        done=False,
        result=None,
    )
    sent: list[dict] = []

    async def send(payload: dict) -> None:
        sent.append(payload)

    async def complete_after_delay():
        await asyncio.sleep(0.01)
        state.events.append({"type": "text", "content": "b"})
        state.done = True
        state.result = {"type": "done", "content": "终态"}
        for w in list(state.waiters):
            w.set()

    completer = asyncio.create_task(complete_after_delay())
    await _forward(state, send, replay=False)
    await completer
    assert [s["type"] for s in sent] == ["text", "done"]
    assert [s.get("content") for s in sent] == ["b", "终态"]


@pytest.mark.asyncio
async def test_forward_stops_on_send_failure_without_raising():
    """send 抛异常（连接断开）时转发终止但不抛（后台任务不受影响）。"""
    state = FakeRunState(
        events=[{"type": "text", "content": "a"}],
        done=False,
        result=None,
    )

    async def send(payload: dict) -> None:
        raise RuntimeError("Cannot call send once a close message has been sent")

    # 不应抛异常
    # 注：replay 必须为 True —— replay=False 时 cursor 从 len(events) 起，
    # 不会发送任何既有事件（done=False 也无终态可发），send 永不触发，
    # _forward 会永远等待 waiter（brief 原测试传 False 会挂死）。
    await _forward(state, send, replay=True)


@pytest.mark.asyncio
async def test_manager_done_resume_path():
    """resume 命中 done：manager.get 返回状态，result 为终态 payload。"""
    manager = ChatTaskManager()
    await manager._cleanup_for_test()

    async def producer(state):
        return {"type": "done", "content": "完整回答"}

    state = manager.start("rs1", "r1", producer)
    assert state is not None
    await state.task
    got = manager.get("rs1")
    assert got is not None
    assert got.done is True
    assert got.result == {"type": "done", "content": "完整回答"}
    await manager._cleanup_for_test()
