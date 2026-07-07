"""routes /chat/stream SSE 端点测试

验证 Task 10 新增的 SSE 流式对话接口：
- Content-Type 为 text/event-stream
- stock 意图事件序列（tool_start/llm_start/text/done），supervisor 事件被过滤
- general 意图（无 tool_start，只有 llm_start/text/done）
- astream_events 抛异常 → SSE error 事件
- 缺失 X-Internal-Token → 403

测试风格与 tests/e2e/test_chat_message_auth.py、test_briefing_morning.py 一致，
使用 httpx.AsyncClient + ASGITransport，mock compile_graph 避免真实 LLM 调用。
mock astream_events 用 async generator function（CD6），不能用 AsyncMock(side_effect=...)
因为 astream_events 是 async generator 不是 coroutine。
"""
import json
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.constants import SSEEventType
from aistock_agent.main import app

_STREAM_URL = "/api/agent/chat/stream"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}


def _parse_sse(text: str) -> list[dict[str, Any]]:
    """解析 SSE 响应文本为事件列表（每行 ``data: {json}``）"""
    events: list[dict[str, Any]] = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _make_chunk(content: str) -> Mock:
    """构造 LangChain chunk mock（纯文本，无 tool_calls）"""
    return Mock(content=content, tool_calls=None, tool_call_chunks=None)


def _make_stream(events: list[dict[str, Any]]) -> Any:
    """把事件列表包装为 async generator function（mock astream_events）"""
    async def _gen(*args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
        for e in events:
            yield e
    return _gen


async def _empty_stream(*args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
    """空 async generator（流立即结束）"""
    return
    yield  # 标记为 async generator（不会执行到此处）


async def _boom_stream(*args: Any, **kwargs: Any) -> AsyncGenerator[dict[str, Any], None]:
    """抛异常的 async generator（首次迭代即抛 RuntimeError）"""
    raise RuntimeError("graph boom")
    yield  # 标记为 async generator（不会执行到此处）


async def _read_sse(resp: httpx.Response) -> str:
    """读取 SSE 流式响应的全部文本"""
    text = ""
    async for line in resp.aiter_lines():
        text += line + "\n"
    return text


_FIXTURE_STOCK_EVENTS: list[dict[str, Any]] = [
    {
        "event": "on_tool_start",
        "name": "get_quote",
        "metadata": {"langgraph_node": "stock_analyst"},
        "data": {"input": {"symbol": "600519"}},
    },
    {
        "event": "on_tool_end",
        "name": "get_quote",
        "metadata": {"langgraph_node": "stock_analyst"},
        "data": {"output": "..."},
    },
    {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "metadata": {"langgraph_node": "stock_analyst"},
        "data": {"chunk": _make_chunk("个股分析中")},
    },
    # supervisor 节点事件（应被过滤，不转发给前端）
    {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "metadata": {"langgraph_node": "supervisor"},
        "data": {"chunk": _make_chunk('{"intent":"stock","symbol":"600519"}')},
    },
]

_FIXTURE_GENERAL_EVENTS: list[dict[str, Any]] = [
    {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "metadata": {"langgraph_node": "general_agent"},
        "data": {"chunk": _make_chunk("这是通用回复")},
    },
]


@pytest.mark.asyncio
async def test_chat_stream_content_type():
    """Content-Type 为 text/event-stream"""
    mock_graph = AsyncMock()
    mock_graph.astream_events = _empty_stream
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _STREAM_URL, json={"message": "你好"}, headers=_VALID_HEADERS,
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_chat_stream_stock_intent_events():
    """stock 意图：过滤 supervisor 事件，转发 tool_start/llm_start/text/done"""
    mock_graph = AsyncMock()
    mock_graph.astream_events = _make_stream(_FIXTURE_STOCK_EVENTS)
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _STREAM_URL, json={"message": "分析 600519"},
                headers=_VALID_HEADERS,
            ) as resp:
                text = await _read_sse(resp)
    events = _parse_sse(text)
    types = [e["type"] for e in events]
    assert SSEEventType.TOOL_START in types
    assert SSEEventType.LLM_START in types
    assert SSEEventType.TEXT in types
    assert events[-1]["type"] == SSEEventType.DONE
    # supervisor 事件被过滤：不应出现 intent JSON 文本
    text_contents = [e.get("content", "") for e in events if e["type"] == SSEEventType.TEXT]
    assert not any("intent" in c for c in text_contents)


@pytest.mark.asyncio
async def test_chat_stream_general_intent_events():
    """general 意图：只有 llm_start/text/done，无 tool_start"""
    mock_graph = AsyncMock()
    mock_graph.astream_events = _make_stream(_FIXTURE_GENERAL_EVENTS)
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _STREAM_URL, json={"message": "你好"},
                headers=_VALID_HEADERS,
            ) as resp:
                text = await _read_sse(resp)
    events = _parse_sse(text)
    types = [e["type"] for e in events]
    assert SSEEventType.TOOL_START not in types
    assert SSEEventType.LLM_START in types
    assert SSEEventType.TEXT in types
    assert events[-1]["type"] == SSEEventType.DONE


@pytest.mark.asyncio
async def test_chat_stream_error_event():
    """astream_events 抛异常 → SSE error 事件"""
    mock_graph = AsyncMock()
    mock_graph.astream_events = _boom_stream
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _STREAM_URL, json={"message": "你好"},
                headers=_VALID_HEADERS,
            ) as resp:
                text = await _read_sse(resp)
    events = _parse_sse(text)
    assert any(e["type"] == SSEEventType.ERROR for e in events)


@pytest.mark.asyncio
async def test_chat_stream_missing_token_403():
    """缺失 X-Internal-Token → 403"""
    with patch("aistock_agent.api.routes.compile_graph",
               side_effect=AssertionError("auth should block")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(_STREAM_URL, json={"message": "你好"})
    assert resp.status_code == 403
