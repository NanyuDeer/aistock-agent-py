"""ws.py 计费落库单测（P10 线 2）。

覆盖：登录（user_id 非空）+ 有 token_usage → 调用 node_api.save_token_usage；
未登录不调用；输出无 token_usage（全 0 归一为 None）不调用。
mock 目标 = aistock_agent.api.ws.node_api.save_token_usage（ws.py 顶部
import 的模块级 node_api 单例）。
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.api import ws as ws_module
from aistock_agent.api.ws import ws_chat


class _FakeWebSocket:
    """最小 WebSocket 替身（与 test_ws_chat_replacement.py 同模式）。"""

    def __init__(self, payloads: list[dict]) -> None:
        self._queue = list(payloads)
        self.sent: list[dict] = []
        self.accepted = False

    async def accept(self) -> None:
        self.accepted = True

    async def receive_json(self) -> dict:
        if not self._queue:
            from fastapi import WebSocketDisconnect

            raise WebSocketDisconnect()
        return self._queue.pop(0)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


class _DoneEventGraph:
    """产生单个 on_chain_end 事件（synth_answer 节点输出）的假图。"""

    def __init__(self, output: dict[str, object]) -> None:
        self._output = output

    async def astream_events(
        self, initial_state: dict[str, object], **kwargs: object
    ) -> object:
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": self._output},
        }


USAGE = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}


@pytest.mark.asyncio
async def test_ws_chat_saves_token_usage_when_logged_in() -> None:
    """登录 + 有 token_usage → save_token_usage 收到正确参数。"""
    ws = _FakeWebSocket([{"message": "分析 600519", "user_id": "u_42", "session_id": "ws_1"}])
    with (
        patch.object(
            ws_module,
            "_select_graph",
            return_value=_DoneEventGraph({"final_response": "回复", "token_usage": USAGE}),
        ),
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock) as mock_save,
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    mock_save.assert_awaited_once_with(
        user_id="u_42",
        session_id="ws_1",
        prompt_tokens=10,
        completion_tokens=20,
        total_tokens=30,
        question="分析 600519",
    )


@pytest.mark.asyncio
async def test_ws_chat_skips_save_when_not_logged_in() -> None:
    """未登录（user_id 空）→ 不调用 save_token_usage。"""
    ws = _FakeWebSocket([{"message": "分析 600519"}])
    with (
        patch.object(
            ws_module,
            "_select_graph",
            return_value=_DoneEventGraph({"final_response": "回复", "token_usage": USAGE}),
        ),
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock) as mock_save,
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_chat_skips_save_when_no_token_usage() -> None:
    """输出无 token_usage（真实链路全 0 归一为 None）→ 不调用。"""
    ws = _FakeWebSocket([{"message": "你好", "user_id": "u_42"}])
    with (
        patch.object(
            ws_module,
            "_select_graph",
            return_value=_DoneEventGraph({"final_response": "我是 AI 投资助手"}),
        ),
        patch.object(ws_module.node_api, "save_token_usage", new_callable=AsyncMock) as mock_save,
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_ws_chat_save_failure_does_not_break_done() -> None:
    """save_token_usage 抛异常（网络层兜底之外的意外）→ 吞掉，DONE 照常发送。"""
    ws = _FakeWebSocket([{"message": "分析 600519", "user_id": "u_42"}])
    with (
        patch.object(
            ws_module,
            "_select_graph",
            return_value=_DoneEventGraph({"final_response": "回复", "token_usage": USAGE}),
        ),
        patch.object(
            ws_module.node_api,
            "save_token_usage",
            new=AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        await ws_chat(ws)  # type: ignore[arg-type]

    done = [s for s in ws.sent if s.get("type") == "done"]
    assert len(done) == 1
    assert done[0]["content"] == "回复"
