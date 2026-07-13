"""routes /briefing/morning SSE 端点测试

Task 4 重构后 /briefing/morning 走 graph 转发（_stream_messages），
不再调用 morning_agent.stream()。测试 mock compile_graph 返回伪图。
"""
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aistock_agent.constants import SSEEventType
from aistock_agent.main import app

_BRIEFING_URL = "/api/agent/briefing/morning"


def _make_chunk(content: str) -> MagicMock:
    """构造 LangChain chunk mock（纯文本，无 tool_calls）"""
    chunk = MagicMock()
    chunk.content = content
    chunk.tool_calls = []
    chunk.tool_call_chunks = []
    return chunk


def _make_mock_graph(events: list[dict[str, object]], final_response: str = "今日晨报内容") -> MagicMock:
    """构造 mock graph（astream_events + aget_state）"""
    async def _astream(*args: object, **kwargs: object) -> AsyncGenerator[dict[str, object], None]:
        for e in events:
            yield e

    mock_graph = MagicMock()
    mock_graph.astream_events = _astream
    mock_final = MagicMock()
    mock_final.values = {"final_response": final_response, "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)
    return mock_graph


def _parse_sse(text: str) -> list[dict[str, object]]:
    """解析 SSE 响应文本为事件列表"""
    events: list[dict[str, object]] = []
    for line in text.split("\n"):
        if line.startswith("data:"):
            events.append(json.loads(line[5:].strip()))
    return events


async def _read_sse(resp: httpx.Response) -> str:
    text = ""
    async for line in resp.aiter_lines():
        text += line + "\n"
    return text


_BRIEFING_EVENTS: list[dict[str, object]] = [
    {
        "event": "on_tool_start",
        "name": "get_global_markets",
        "metadata": {"langgraph_node": "morning_agent"},
        "data": {"input": {}},
    },
    {
        "event": "on_tool_end",
        "name": "get_global_markets",
        "metadata": {"langgraph_node": "morning_agent"},
        "data": {},
    },
    {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "metadata": {"langgraph_node": "morning_agent"},
        "data": {"chunk": _make_chunk("今日晨报内容")},
    },
]


@pytest.fixture(autouse=True)
def _cleanup_briefing_queue():
    """清理 /briefing/morning 固定 session_id 的队列，避免跨测试干扰。"""
    from aistock_agent.api import routes as routes_mod
    routes_mod._event_queues.pop("briefing_morning", None)
    yield
    routes_mod._event_queues.pop("briefing_morning", None)


@pytest.mark.asyncio
async def test_briefing_morning_content_type():
    """响应 Content-Type 必须是 text/event-stream"""
    mock_graph = _make_mock_graph(_BRIEFING_EVENTS)
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", _BRIEFING_URL) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_briefing_morning_sse_events():
    """SSE 数据行可解析为预期 JSON 事件序列

    /briefing/morning 走 _stream_messages (filter_type="text")：
    仅 text + done，tool 事件被过滤。
    """
    mock_graph = _make_mock_graph(_BRIEFING_EVENTS)
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", _BRIEFING_URL) as resp:
                text = await _read_sse(resp)

    events = _parse_sse(text)
    types = [e["type"] for e in events]
    # messages 流：仅 text + done，tool 事件被过滤
    assert SSEEventType.TEXT in types
    assert types[-1] == SSEEventType.DONE
    assert SSEEventType.TOOL_START not in types

    # done 携带 final_response
    done_event = events[-1]
    assert done_event["final_response"] == "今日晨报内容"

    # text 内容正确
    text_event = next(e for e in events if e["type"] == SSEEventType.TEXT)
    assert text_event["content"] == "今日晨报内容"


@pytest.mark.asyncio
async def test_briefing_morning_error_event():
    """astream_events 抛异常 → SSE error 事件"""
    async def _boom_stream(*args: object, **kwargs: object) -> AsyncGenerator[dict[str, object], None]:
        raise RuntimeError("LLM unavailable")
        yield  # 标记为 async generator

    mock_graph = MagicMock()
    mock_graph.astream_events = _boom_stream

    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", _BRIEFING_URL) as resp:
                text = await _read_sse(resp)

    events = _parse_sse(text)
    assert events[0]["type"] == SSEEventType.ERROR
    assert "LLM unavailable" in events[0]["message"]
