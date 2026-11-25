"""ws.py 对话入口注入 user_profile 单测（Phase 4-3 Task 3）。

覆盖（对齐 task-3 brief §测试）：
- ① user_id 非空 → 拉取 profile 注入 initial_state["user_profile"]
- ② user_id 为空（未登录）→ 不拉取、不注入（字段保持未设置）
- ③ 拉取失败（get_user_profile 返回 None）→ 跳过注入，对话正常 DONE（"永不 500"）
"""
import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import WebSocketDisconnect

from aistock_agent.api import ws as ws_module
from aistock_agent.api.ws import ws_chat

PROFILE = {
    "user_id": "u_42",
    "nickname": "小王",
    "investment_preferences": ["白酒", "新能源"],
    "risk_tolerance": "conservative",
}


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


def _message(user_id: str | None) -> dict:
    return {
        "message": "贵州茅台今天怎么样",
        "session_id": "s_p43",
        "run_id": "r_p43",
        "user_id": user_id,
    }


@pytest.mark.asyncio
async def test_user_id_present_injects_user_profile() -> None:
    """登录态（user_id 非空）→ 拉取 profile 注入 initial_state['user_profile']。"""
    captured: list[dict] = []
    ws = _MessageThenHangSocket(_message("u_42"))
    with (
        patch.object(ws_module, "_select_graph", return_value=_DoneGraph(captured)),
        patch.object(
            ws_module.node_api, "get_user_profile", new=AsyncMock(return_value=PROFILE)
        ) as get_profile,
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    get_profile.assert_awaited_once_with("u_42")
    assert len(captured) == 1
    assert captured[0]["user_profile"] == PROFILE
    assert ws.sent[-1]["type"] == "done", "注入不改变正常对话终态"


@pytest.mark.asyncio
async def test_anonymous_skips_profile_fetch() -> None:
    """未登录（user_id 为空）→ 不拉取 profile，字段保持未设置。"""
    captured: list[dict] = []
    ws = _MessageThenHangSocket(_message(None))
    with (
        patch.object(ws_module, "_select_graph", return_value=_DoneGraph(captured)),
        patch.object(ws_module.node_api, "get_user_profile", new=AsyncMock()) as get_profile,
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    get_profile.assert_not_awaited()
    assert len(captured) == 1
    assert captured[0].get("user_profile") is None
    assert ws.sent[-1]["type"] == "done"


@pytest.mark.asyncio
async def test_profile_fetch_failure_skips_injection_and_does_not_block() -> None:
    """拉取失败（返回 None）→ 跳过注入，对话正常完成（'永不 500'）。"""
    captured: list[dict] = []
    ws = _MessageThenHangSocket(_message("u_42"))
    with (
        patch.object(ws_module, "_select_graph", return_value=_DoneGraph(captured)),
        patch.object(
            ws_module.node_api, "get_user_profile", new=AsyncMock(return_value=None)
        ) as get_profile,
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    get_profile.assert_awaited_once_with("u_42")
    assert len(captured) == 1
    assert captured[0].get("user_profile") is None
    assert ws.sent[-1]["type"] == "done"
