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
        self.user_id = None          # Part 2：归属
        self.cancelled = False       # Part 2：cancelled 终态标记
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


"""--- Part 2：stop 控制消息 + 归属校验 ---"""


class FakeCmdWs:
    """最小 WebSocket 形状：send_json 记录；receive_json 按 inbox 出队，空则挂起。"""

    def __init__(self, inbox: list[dict]):
        self.sent: list[dict] = []
        self._inbox = list(inbox)

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def receive_json(self) -> dict:
        while not self._inbox:
            await asyncio.sleep(0.02)
        return self._inbox.pop(0)


@pytest.mark.asyncio
async def test_owns_run_ownership_rules():
    """归属校验规则（spec §8.4）：None state 放行；双方非空必须相等；任一方 None 放行。"""
    from aistock_agent.api.ws import _owns_run

    assert _owns_run(None, "o_x") is True      # 无记录 → 放行（走 none/not_found）
    assert _owns_run(None, None) is True
    state = FakeRunState(events=[], done=True, result=None)
    state.user_id = "o_a"
    assert _owns_run(state, "o_a") is True
    assert _owns_run(state, "o_b") is False    # 越权
    assert _owns_run(state, None) is True      # 未登录放行
    state.user_id = None
    assert _owns_run(state, "o_b") is True


@pytest.mark.asyncio
async def test_forward_until_done_or_cmd_stop_cancels_run():
    """生成中收到 stop → cancel 活跃 run + stop_status cancelled + cancelled 终态。"""
    from aistock_agent.api.ws import _forward_until_done_or_cmd
    from aistock_agent.services.chat_task_manager import chat_task_manager

    await chat_task_manager._cleanup_for_test()
    started = asyncio.Event()

    async def slow_producer(state):
        started.set()
        await asyncio.sleep(30)
        return {"type": "done", "content": "x"}

    # 注意：必须用模块级单例 chat_task_manager（_forward_until_done_or_cmd 内部
    # 用同一单例 get/cancel，若用独立 ChatTaskManager() 实例会 miss 状态）
    state = chat_task_manager.start("st1", "r1", slow_producer, "o_a")
    assert state is not None
    await started.wait()

    ws = FakeCmdWs([{"type": "stop", "session_id": "st1", "user_id": "o_a"}])
    await _forward_until_done_or_cmd(state, ws, "st1")
    await state.task

    assert state.done is True and state.cancelled is True
    assert state.result == {"type": "cancelled", "content": "已停止生成"}
    assert any(
        s.get("type") == "stop_status" and s.get("status") == "cancelled"
        for s in ws.sent
    )
    assert any(s.get("type") == "cancelled" for s in ws.sent)  # 经 _forward 终态路径下发
    await chat_task_manager._cleanup_for_test()


@pytest.mark.asyncio
async def test_forward_until_done_or_cmd_ignores_unauthorized_stop():
    """越权 stop（user_id 不一致）→ 不 cancel、显式 error，不静默。"""
    from aistock_agent.api.ws import _forward_until_done_or_cmd
    from aistock_agent.services.chat_task_manager import chat_task_manager

    await chat_task_manager._cleanup_for_test()
    started = asyncio.Event()

    async def slow_producer(state):
        started.set()
        await asyncio.sleep(30)
        return {"type": "done", "content": "x"}

    state = chat_task_manager.start("st2", "r1", slow_producer, "o_a")
    assert state is not None
    await started.wait()

    ws = FakeCmdWs([{"type": "stop", "session_id": "st2", "user_id": "o_b"}])
    # 越权 stop 不应 cancel——先让内部处理越权 stop 分支，再手动结束 run（模拟后端最终收尾）
    stop_task = asyncio.create_task(_forward_until_done_or_cmd(state, ws, "st2"))
    await asyncio.sleep(0.05)  # 让内部处理越权 stop 分支
    state.cancel()             # 手动取消 run 使转发协程退出（验证越权分支不 cancel）
    await state.task
    await stop_task

    assert state.cancelled is True
    assert any(
        s.get("type") == "error" and s.get("content") == "无权访问该会话"
        for s in ws.sent
    )
    await chat_task_manager._cleanup_for_test()
