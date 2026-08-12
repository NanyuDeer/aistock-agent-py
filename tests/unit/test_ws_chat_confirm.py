"""ws.py 交互式确认两阶段编排单测（Phase 4-2，改进 13）。

覆盖（对齐 task-4 brief §测试）：
- ① 阶段 1：图输出含 confirm → 终态负载 confirm_request（非 DONE），并携带
     request_id/question/options/context；confirm_response(choice) → 阶段 2 携带
     confirm_choice 重跑同 session 图 → 正常 DONE（transient 字段归零）
- ② _wait_confirm_response：收到匹配 choice → 返回归一化 choice（label 反查 options）
- ③ request_id 不匹配 → 忽略继续等；匹配才返回
- ④ 归属拒绝（越权 user_id）→ 发送 ERROR「无权访问该会话」继续等
- ⑤ 60s 超时（mock 缩短）→ 返回 None，recv 收尾无残留（问题 18）
- ⑥ choice=="none" → 返回 None（按确认超时重跑）
"""
import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocketDisconnect

from aistock_agent.api import ws as ws_module
from aistock_agent.api.ws import ws_chat
from aistock_agent.services.chat_task_manager import ChatRunState

CONFIRM = {
    "question": "我想了解一下贵州茅台和五粮液",
    "options": [
        {"key": "600519", "label": "贵州茅台(600519)"},
        {"key": "000858", "label": "五粮液(000858)"},
        {"key": "none", "label": "都不是"},
    ],
}


class _FakeWebSocket:
    """最小 WebSocket 替身：按队列吐出请求，捕获 send_json。"""

    def __init__(self, payloads: list[dict]) -> None:
        self._queue = list(payloads)
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        if self._queue:
            return self._queue.pop(0)
        raise WebSocketDisconnect()

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _HangWebSocket:
    """队列空时挂起（60s 超时测试用）；跟踪 recv 活跃态以校验收尾无残留。"""

    def __init__(self, payloads: list[dict] | None = None) -> None:
        self._queue = list(payloads or [])
        self.sent: list[dict] = []
        self.recv_active = False

    async def receive_json(self) -> dict:
        if self._queue:
            return self._queue.pop(0)
        self.recv_active = True
        try:
            await asyncio.Event().wait()
            return {"type": "pong"}
        finally:
            self.recv_active = False

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _ScriptedConfirmSocket:
    """三阶段脚本 WebSocket：1) 聊天消息 → 2) 挂起（阶段 1 生成中 recv 被 cancel）
    → 3) confirm_response（仅在前端收到 confirm_request 后才会发送，时序对齐真实链路）。"""

    def __init__(self, message: dict, confirm_response: dict) -> None:
        self._message = message
        self._confirm_response = confirm_response
        self._calls = 0
        self.sent: list[dict] = []
        self.accepted = False
        self.recv_active = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        self._calls += 1
        if self._calls == 1:
            return self._message
        if self._calls == 2:
            # 阶段 1 生成期间的 recv：挂起，等 _forward_until_done_or_cmd cancel
            self.recv_active = True
            try:
                await asyncio.Event().wait()
                return {}
            finally:
                self.recv_active = False
        if self._calls == 3:
            return self._confirm_response
        raise WebSocketDisconnect()

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _TwoPhaseGraph:
    """状态假图：第 1 次运行产出 confirm 终态，后续运行产出普通 done。"""

    def __init__(
        self, captured: list[dict], confirm: dict, done_content: str = "阶段 2 回答"
    ) -> None:
        self.captured = captured
        self._confirm = confirm
        self._done_content = done_content
        self._calls = 0

    async def astream_events(
        self, initial_state: dict[str, object], **kwargs: object
    ) -> AsyncIterator[dict[str, object]]:
        self.captured.append(initial_state)
        self._calls += 1
        if self._calls == 1:
            yield {
                "event": "on_chain_end",
                "name": "synth_answer",
                "data": {"output": {"final_response": "", "confirm": self._confirm}},
            }
        else:
            yield {
                "event": "on_chain_end",
                "name": "synth_answer",
                "data": {"output": {"final_response": self._done_content}},
            }


class _DoneEventGraph:
    """产生单个 on_chain_end 事件（synth_answer 节点输出）的假图。"""

    def __init__(self, output: dict[str, object]) -> None:
        self._output = output

    async def astream_events(
        self, initial_state: dict[str, object], **kwargs: object
    ) -> AsyncIterator[dict[str, object]]:
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": self._output},
        }


def _owned_state(user_id: str) -> ChatRunState:
    """构造已归属的 ChatRunState（_wait_confirm_response 归属校验用）。"""
    task = asyncio.create_task(asyncio.sleep(0))
    state = ChatRunState(session_id="s1", run_id="r1", task=task, user_id=user_id)
    state.done = True
    state.result = {"type": "done", "content": "hi"}
    return state


# ── ① 阶段 1 confirm_request 终态 + 阶段 2 confirm_choice 重跑 ────────────


@pytest.mark.asyncio
async def test_ws_chat_two_phase_confirm_choice_rerun() -> None:
    """阶段 1 confirm_request → confirm_response(choice) → 阶段 2 重跑 → DONE。"""
    captured: list[dict] = []
    ws = _ScriptedConfirmSocket(
        {
            "message": "我想了解一下贵州茅台和五粮液",
            "session_id": "s_confirm",
            "run_id": "r_confirm",
            "user_id": "u_42",
        },
        {
            "type": "confirm_response",
            "request_id": "r_confirm",
            "choice": "600519",
            "user_id": "u_42",
        },
    )
    with (
        patch.object(ws_module, "_select_graph", return_value=_TwoPhaseGraph(captured, CONFIRM)),
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    types = [s.get("type") for s in ws.sent]
    assert "confirm_request" in types, f"缺少 confirm_request: {types}"
    assert "done" not in types[:-1], "阶段 1 终态应为 confirm_request 而非 DONE"
    assert types[-1] == "done", "阶段 2 应正常 DONE"

    confirm_req = next(s for s in ws.sent if s.get("type") == "confirm_request")
    assert confirm_req["request_id"] == "r_confirm"
    assert confirm_req["question"] == CONFIRM["question"]
    assert confirm_req["options"] == CONFIRM["options"]
    assert confirm_req["context"] == {"session_id": "s_confirm"}

    # 阶段 2 initial_state：携带 confirm_choice，transient 字段归零
    assert len(captured) == 2
    assert captured[0].get("confirm_choice") is None  # 阶段 1 无输入信号（入口归零）
    assert captured[1]["confirm_choice"] == {"symbol": "600519", "label": "贵州茅台(600519)"}
    assert captured[1].get("confirm_timeout") is None
    assert captured[1]["confirm"] is None
    assert captured[1]["deep_source"] is None
    assert captured[1]["final_response"] is None
    assert captured[1]["goals"] is None
    assert captured[1]["general_source"] is None


# ── ①' _run_chat_graph_to_events：confirm 终态负载（非 DONE）─────────────


@pytest.mark.asyncio
async def test_run_chat_graph_confirm_request_terminal_payload() -> None:
    """图终态输出含 confirm → producer 返回 confirm_request（不是 DONE、不落计费）。"""
    state = _owned_state("u_42")
    graph = _DoneEventGraph({"final_response": "", "confirm": CONFIRM})
    with patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock) as mock_save:
        result = await ws_module._run_chat_graph_to_events(
            state, graph, {"message": "x"}, "msg", "s1", "u_42", run_id="run_abc"
        )
    mock_save.assert_not_awaited()  # 阶段 1 不落计费（阶段 2 是新一轮调用）
    assert result is not None
    assert result["type"] == "confirm_request"
    assert result["request_id"] == "run_abc"
    assert result["question"] == CONFIRM["question"]
    assert result["options"] == CONFIRM["options"]
    assert result["context"] == {"session_id": "s1"}


# ── ② _wait_confirm_response：匹配 choice → 归一化返回 ────────────────────


@pytest.mark.asyncio
async def test_wait_confirm_response_returns_normalized_choice() -> None:
    """收到匹配 confirm_response → 返回 {"symbol": key, "label": 反查 options}。"""
    ws = _FakeWebSocket(
        [{"type": "confirm_response", "request_id": "r1", "choice": "600519"}]
    )
    result = await ws_module._wait_confirm_response(None, ws, "s1", "r1", CONFIRM)
    assert result == {"symbol": "600519", "label": "贵州茅台(600519)"}


# ── ③ request_id 不匹配忽略 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_confirm_response_ignores_wrong_request_id() -> None:
    """request_id 不匹配 → 忽略继续等；匹配才返回 choice。"""
    ws = _FakeWebSocket(
        [
            {"type": "confirm_response", "request_id": "wrong", "choice": "600519"},
            {"type": "confirm_response", "request_id": "r1", "choice": "600519"},
        ]
    )
    result = await ws_module._wait_confirm_response(None, ws, "s1", "r1", CONFIRM)
    assert result == {"symbol": "600519", "label": "贵州茅台(600519)"}


# ── ④ 归属拒绝 ERROR ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_confirm_response_rejects_unauthorized_user() -> None:
    """越权 user_id → ERROR「无权访问该会话」继续等；后匹配 choice 正常返回。"""
    ws = _FakeWebSocket(
        [
            {
                "type": "confirm_response",
                "request_id": "r1",
                "choice": "600519",
                "user_id": "attacker",
            },
            {
                "type": "confirm_response",
                "request_id": "r1",
                "choice": "600519",
                "user_id": "u_42",
            },
        ]
    )
    state = _owned_state("u_42")
    result = await ws_module._wait_confirm_response(state, ws, "s1", "r1", CONFIRM)
    assert result == {"symbol": "600519", "label": "贵州茅台(600519)"}
    errors = [s for s in ws.sent if s.get("type") == "error"]
    assert len(errors) == 1
    assert errors[0]["content"] == "无权访问该会话"


# ── ⑤ 60s 超时（mock 缩短）→ None + recv 收尾无残留 ──────────────────────


@pytest.mark.asyncio
async def test_wait_confirm_response_timeout_returns_none() -> None:
    """60s 超时（mock 缩短）→ 返回 None；recv 已收尾（问题 18：无残留挂起 recv）。"""
    ws = _HangWebSocket([])
    with patch.object(ws_module, "_CONFIRM_TIMEOUT_SEC", 0.05):
        result = await ws_module._wait_confirm_response(None, ws, "s1", "r1", CONFIRM)
    assert result is None
    assert ws.recv_active is False, "超时后必须 await 收尾 cancel 的 recv（问题 18）"


# ── ⑥ choice=="none" → None（按确认超时重跑）────────────────────────────


@pytest.mark.asyncio
async def test_wait_confirm_response_choice_none_returns_none() -> None:
    """用户点「都不是」（choice=none）→ 返回 None，按确认超时重跑。"""
    ws = _FakeWebSocket(
        [{"type": "confirm_response", "request_id": "r1", "choice": "none"}]
    )
    result = await ws_module._wait_confirm_response(None, ws, "s1", "r1", CONFIRM)
    assert result is None
