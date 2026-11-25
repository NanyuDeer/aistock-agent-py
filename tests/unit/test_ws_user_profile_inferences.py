"""对话入口注入 user_profile_inferences 单测（用户画像自动挖掘一期 Task 6）。

覆盖四个入口（对齐 plan Task 6「入口无条件显式赋值」硬约束）：
- ws.py `_handle_user_message`（主消息）：登录注入 / 匿名显式 None / 拉取失败显式 None
- ws.py `_run_confirm_stage2`（确认阶段 2）：同样无条件注入
- routes.py `chat_message`（HTTP 非流式）：登录注入 / 匿名显式 None
- routes.py `chat_stream_messages`（SSE）：登录注入

关键回归点：匿名或失败时必须写 `None`（不能跳过赋值），否则 checkpointer 会把上一轮
的推断带进本轮（Phase 4-3 user_profile 已踩过该坑）。
"""
import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocketDisconnect

from aistock_agent.api import routes as routes_module
from aistock_agent.api import ws as ws_module
from aistock_agent.api.ws import ws_chat
from aistock_agent.schemas.chat import ChatRequest

PROFILE = {
    "user_id": "u_42",
    "nickname": "小王",
    "investment_preferences": ["白酒", "新能源"],
    "risk_tolerance": "conservative",
}

INFERENCES = [
    {
        "dimension": "preferred_sectors",
        "value": {"items": [{"name": "白酒", "stock_count": 3, "weight": 0.19}], "total": 16},
        "confidence": 0.7,
        "sample_size": 16,
        "window_days": None,
        "inferred_at": "2026-11-05T00:30:00Z",
    }
]


class _MessageThenHangSocket:
    """最小 WebSocket 替身：第 1 次返回 message，之后挂起 recv（等 _forward cancel），最后断开。"""

    def __init__(self, message: dict) -> None:
        self._message = message
        self.sent: list[dict] = []
        self._calls = 0

    async def accept(self) -> None:
        pass

    async def receive_json(self) -> dict:
        self._calls += 1
        if self._calls == 1:
            return self._message
        if self._calls == 2:
            # 生成期间的 recv：挂起，等 _forward_until_done_or_cmd cancel 收尾
            self.recv_active = True
            try:
                await asyncio.Event().wait()
                return {}
            finally:
                self.recv_active = False
        raise WebSocketDisconnect()

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _DoneGraph:
    """产生单个 synth_answer on_chain_end（final_response）的假图，并捕获 initial_state。"""

    def __init__(self, captured: list[dict]) -> None:
        self.captured = captured

    async def astream_events(
        self, initial_state: dict[str, object], **kwargs: object
    ) -> AsyncIterator[dict[str, object]]:
        self.captured.append(initial_state)
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "贵州茅台今日震荡上行。"}},
        }


class _HttpGraph:
    """routes.chat_message 用的假图（astream updates 模式）。"""

    def __init__(self, captured: list[dict]) -> None:
        self.captured = captured

    async def astream(
        self,
        initial_state: dict[str, object],
        config: dict[str, object] | None = None,
        stream_mode: str | None = None,
    ) -> AsyncIterator[dict[str, object]]:
        self.captured.append(initial_state)
        yield {"synth_answer": {"final_response": "贵州茅台今日震荡上行。"}}


def _message(user_id: str | None) -> dict:
    return {
        "message": "贵州茅台今天怎么样",
        "session_id": "s_p1",
        "run_id": "r_p1",
        "user_id": user_id,
    }


def _patch_ws_entry(inferences_result: object) -> tuple:
    """ws 主入口公共 patch：图 / 画像 / 推断 / 计费。"""
    return (
        patch.object(ws_module.node_api, "get_user_profile", new=AsyncMock(return_value=PROFILE)),
        patch.object(
            ws_module.node_api,
            "get_user_profile_inferences",
            new=AsyncMock(return_value=inferences_result),
        ),
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock),
    )


@pytest.mark.asyncio
async def test_ws_user_id_present_injects_inferences() -> None:
    """ws 主入口登录态 → 按 user_id 拉取并注入 initial_state['user_profile_inferences']。"""
    captured: list[dict] = []
    ws = _MessageThenHangSocket(_message("u_42"))
    p1, p2, p3 = _patch_ws_entry(INFERENCES)
    with (
        patch.object(ws_module, "_select_graph", return_value=_DoneGraph(captured)),
        p1,
        p2 as get_inferences,
        p3,
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    get_inferences.assert_awaited_once_with("u_42")
    assert len(captured) == 1
    assert captured[0]["user_profile_inferences"] == INFERENCES
    assert ws.sent[-1]["type"] == "done", "注入不改变正常对话终态"


@pytest.mark.asyncio
async def test_ws_anonymous_explicitly_writes_none() -> None:
    """ws 主入口未登录 → 不拉取，且必须显式写 None 覆盖 checkpoint 旧值。"""
    captured: list[dict] = []
    ws = _MessageThenHangSocket(_message(None))
    p1, p2, p3 = _patch_ws_entry(INFERENCES)
    with (
        patch.object(ws_module, "_select_graph", return_value=_DoneGraph(captured)),
        p1,
        p2 as get_inferences,
        p3,
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    get_inferences.assert_not_awaited()
    assert len(captured) == 1
    # 「键存在且为 None」= 无条件显式赋值（区别于跳过赋值导致跨轮残留）
    assert "user_profile_inferences" in captured[0]
    assert captured[0]["user_profile_inferences"] is None
    assert ws.sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_ws_inferences_fetch_failure_writes_none_and_does_not_block() -> None:
    """ws 主入口拉取失败（None）→ 显式 None，对话正常完成（"永不阻断主流程"）。"""
    captured: list[dict] = []
    ws = _MessageThenHangSocket(_message("u_42"))
    p1, p2, p3 = _patch_ws_entry(None)
    with (
        patch.object(ws_module, "_select_graph", return_value=_DoneGraph(captured)),
        p1,
        p2 as get_inferences,
        p3,
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    get_inferences.assert_awaited_once_with("u_42")
    assert len(captured) == 1
    assert captured[0]["user_profile_inferences"] is None
    assert ws.sent[-1]["type"] == "done"


class _CapturedStart:
    """替身 chat_task_manager.start：只捕获 producer，不真正起后台任务。"""

    def __init__(self) -> None:
        self.producer = None

    def __call__(
        self,
        session_id: str,
        run_id: str,
        producer: object,
        user_id: str | None = None,
    ) -> object:
        self.producer = producer
        return object()


@pytest.mark.asyncio
async def test_ws_confirm_stage2_injects_inferences() -> None:
    """ws 确认阶段 2（_run_confirm_stage2）同样无条件注入推断（对齐阶段 1）。"""
    captured_state: list[dict] = []
    starter = _CapturedStart()

    async def fake_run(
        st: object,
        graph: object,
        initial_state: dict,
        message: str,
        session_id: str,
        uid: str | None,
        rid: str,
    ) -> dict:
        captured_state.append(initial_state)
        return {"type": "done"}

    with (
        patch.object(ws_module.chat_task_manager, "clear_pending_confirm"),
        patch.object(ws_module.chat_task_manager, "start", new=starter),
        patch.object(ws_module, "_forward_until_done_or_cmd", new=AsyncMock()),
        patch.object(ws_module, "_select_graph", return_value=object()),
        patch.object(ws_module, "_run_chat_graph_to_events", new=fake_run),
        patch.object(
            ws_module.node_api, "get_user_profile", new=AsyncMock(return_value=PROFILE)
        ),
        patch.object(
            ws_module.node_api,
            "get_user_profile_inferences",
            new=AsyncMock(return_value=INFERENCES),
        ) as get_inferences,
    ):
        await ws_module._run_confirm_stage2(
            _MessageThenHangSocket(_message("u_42")),
            "s_p1",
            "贵州茅台今天怎么样",
            "u_42",
            False,
            None,
        )
        assert starter.producer is not None
        await starter.producer(SimpleNamespace())  # type: ignore[operator]

    get_inferences.assert_awaited_once_with("u_42")
    assert len(captured_state) == 1
    assert captured_state[0]["user_profile_inferences"] == INFERENCES


@pytest.mark.asyncio
async def test_http_chat_message_injects_inferences() -> None:
    """routes.chat_message（HTTP 非流式）登录态 → 注入推断。"""
    captured: list[dict] = []
    req = ChatRequest(message="贵州茅台今天怎么样", session_id="s_http", user_id="u_42")
    with (
        patch.object(routes_module, "_select_graph", return_value=_HttpGraph(captured)),
        patch.object(
            routes_module.node_api, "get_user_profile", new=AsyncMock(return_value=PROFILE)
        ),
        patch.object(
            routes_module.node_api,
            "get_user_profile_inferences",
            new=AsyncMock(return_value=INFERENCES),
        ) as get_inferences,
    ):
        resp = await routes_module.chat_message(req, None)

    get_inferences.assert_awaited_once_with("u_42")
    assert resp.content == "贵州茅台今日震荡上行。"
    assert len(captured) == 1
    assert captured[0]["user_profile_inferences"] == INFERENCES


@pytest.mark.asyncio
async def test_http_chat_message_anonymous_explicitly_writes_none() -> None:
    """routes.chat_message 匿名 → 不拉取且显式写 None（防跨轮残留）。"""
    captured: list[dict] = []
    req = ChatRequest(message="贵州茅台今天怎么样", session_id="s_http", user_id=None)
    with (
        patch.object(routes_module, "_select_graph", return_value=_HttpGraph(captured)),
        patch.object(
            routes_module.node_api, "get_user_profile", new=AsyncMock(return_value=None)
        ),
        patch.object(
            routes_module.node_api, "get_user_profile_inferences", new=AsyncMock()
        ) as get_inferences,
    ):
        await routes_module.chat_message(req, None)

    get_inferences.assert_not_awaited()
    assert len(captured) == 1
    assert "user_profile_inferences" in captured[0]
    assert captured[0]["user_profile_inferences"] is None


class _FakeEventSourceResponse:
    """替身 EventSourceResponse：保留 generator 供测试手动驱动。"""

    def __init__(self, generator: object) -> None:
        self.generator = generator


@pytest.mark.asyncio
async def test_sse_chat_stream_injects_inferences() -> None:
    """routes.chat_stream_messages（SSE）登录态 → 注入推断。"""
    captured: list[dict] = []

    async def fake_stream(
        graph: object, initial_state: dict, session_id: str
    ) -> AsyncIterator[dict[str, str]]:
        captured.append(initial_state)
        if False:  # pragma: no cover - 使函数成为异步生成器（不实际产出事件）
            yield {}

    req = ChatRequest(message="贵州茅台今天怎么样", session_id="s_sse", user_id="u_42")
    with (
        patch.object(routes_module, "_select_graph", return_value=object()),
        patch.object(routes_module, "_stream_messages", new=fake_stream),
        patch.object(routes_module, "EventSourceResponse", new=_FakeEventSourceResponse),
        patch.object(
            routes_module.node_api, "get_user_profile", new=AsyncMock(return_value=PROFILE)
        ),
        patch.object(
            routes_module.node_api,
            "get_user_profile_inferences",
            new=AsyncMock(return_value=INFERENCES),
        ) as get_inferences,
    ):
        resp = await routes_module.chat_stream_messages(req, None)
        with pytest.raises(StopAsyncIteration):
            await resp.generator.__anext__()  # type: ignore[attr-defined]

    get_inferences.assert_awaited_once_with("u_42")
    assert len(captured) == 1
    assert captured[0]["user_profile_inferences"] == INFERENCES
