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


@pytest.mark.asyncio
async def test_sse_done_confirm_falls_back_to_clarification() -> None:
    """SSE 降级路径遇 confirm 终态（WS 专属两阶段交互）→ DONE.final_response 回退澄清文本。

    Phase 4-2（改进 13）：confirm 是 WS 专属协议，SSE 无交互能力但 qa_router
    仍可能触发（传输无关）；空 final_response + confirm 存在时必须降级为既有
    澄清文本，避免前端拿到空回答（严格劣化回归）。
    """
    graph = MagicMock()
    graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(
            values={
                "final_response": "",
                "analysis_reports": {},
                "confirm": {
                    "request_id": "r1",
                    "question": "您想了解哪只股票？",
                    "options": [{"key": "600519", "label": "贵州茅台"}],
                },
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
    assert done_events[0]["final_response"] == "请提供 6 位股票代码后重试。"


@pytest.mark.asyncio
async def test_sse_done_normal_response_unchanged_when_confirm_absent() -> None:
    """无 confirm 时 DONE.final_response 原样透出（正常回答/既有澄清不受影响）。"""
    graph = MagicMock()
    graph.aget_state = AsyncMock(
        return_value=SimpleNamespace(
            values={
                "final_response": "贵州茅台今日走势震荡上行。",
                "analysis_reports": {},
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
    assert done_events[0]["final_response"] == "贵州茅台今日走势震荡上行。"
