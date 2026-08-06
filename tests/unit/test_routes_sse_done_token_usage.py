"""routes.py _stream_messages SSE DONE 附带 token_usage/cards 单测（P10 线 2）。

直接驱动 _stream_messages：mock 队列预置 None 哨兵（走 DONE 分支），
graph.aget_state 返回含 token_usage/cards 的 values。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.api import routes as routes_module
from aistock_agent.api.routes import _stream_messages


def _make_queue_with_done() -> asyncio.Queue[object | None]:
    queue: asyncio.Queue[object | None] = asyncio.Queue()
    queue.put_nowait(None)  # 哨兵：graph 结束 → DONE 分支
    return queue


@pytest.mark.asyncio
async def test_sse_done_includes_token_usage_and_cards() -> None:
    """SSE DONE 从 final_state.values 附带 token_usage/cards。"""
    graph = MagicMock()
    graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(
            values={
                "final_response": "回复",
                "analysis_reports": {},
                "token_usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
                "cards": None,
            }
        )
    )
    queue = _make_queue_with_done()
    with patch.object(
        routes_module, "_ensure_message_queue", return_value=(queue, False)
    ):
        events = [e async for e in _stream_messages(graph, {"messages": []}, "sse_1")]

    done_events = [e for e in events if e.get("type") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["token_usage"] == {
        "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
    }
    assert done_events[0]["cards"] is None


@pytest.mark.asyncio
async def test_sse_done_missing_fields_default_none() -> None:
    """final_state.values 无两字段（旧路径）→ DONE 为 None（null 兼容）。"""
    graph = MagicMock()
    graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(
            values={"final_response": "回复", "analysis_reports": {}}
        )
    )
    queue = _make_queue_with_done()
    with patch.object(
        routes_module, "_ensure_message_queue", return_value=(queue, False)
    ):
        events = [e async for e in _stream_messages(graph, {"messages": []}, "sse_1")]

    done_events = [e for e in events if e.get("type") == "done"]
    assert done_events[0]["token_usage"] is None
    assert done_events[0]["cards"] is None
