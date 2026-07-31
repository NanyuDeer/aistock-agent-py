"""routes /chat/stream/messages + /chat/stream/updates SSE 端点测试

验证 Task 4 新增的双流 SSE 接口：
- /chat/stream/messages: Content-Type text/event-stream, 仅 TEXT + LLM_START + DONE
- /chat/stream/updates: 仅 TOOL_START/END + AGENT_SWITCH + DONE
- supervisor 节点事件被过滤
- astream_events 抛异常 → SSE error 事件
- 缺失 X-Internal-Token → 403

测试风格与 tests/e2e/test_chat_message_auth.py、test_briefing_morning.py 一致，
使用 httpx.AsyncClient + ASGITransport，mock compile_graph 避免真实 LLM 调用。
mock astream_events 用 async generator function，不能用 AsyncMock(side_effect=...)
因为 astream_events 是 async generator 不是 coroutine。
"""
import json
from collections.abc import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.constants import SSEEventType
from aistock_agent.main import app

_MESSAGES_URL = "/api/agent/chat/stream/messages"
_UPDATES_URL = "/api/agent/chat/stream/updates"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}


def _parse_sse(text: str) -> list[dict[str, object]]:
    """解析 SSE 响应文本为事件列表（每行 ``data: {json}``）"""
    events: list[dict[str, object]] = []
    for line in text.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[6:]))
    return events


def _make_chunk(content: str) -> Mock:
    """构造 LangChain chunk mock（纯文本，无 tool_calls）"""
    return Mock(content=content, tool_calls=None, tool_call_chunks=None)


def _make_stream(events: list[dict[str, object]]) -> object:
    """把事件列表包装为 async generator function（mock astream_events）"""
    async def _gen(*args: object, **kwargs: object) -> AsyncGenerator[dict[str, object], None]:
        for e in events:
            yield e
    return _gen


async def _empty_stream(*args: object, **kwargs: object) -> AsyncGenerator[dict[str, object], None]:
    """空 async generator（流立即结束）"""
    return
    yield  # 标记为 async generator（不会执行到此处）


async def _boom_stream(*args: object, **kwargs: object) -> AsyncGenerator[dict[str, object], None]:
    """抛异常的 async generator（首次迭代即抛 RuntimeError）"""
    raise RuntimeError("graph boom")
    yield  # 标记为 async generator（不会执行到此处）


async def _read_sse(resp: httpx.Response) -> str:
    """读取 SSE 流式响应的全部文本"""
    text = ""
    async for line in resp.aiter_lines():
        text += line + "\n"
    return text


def _make_mock_graph(
    astream_events_fn: object,
    final_response: str = "mocked 最终回复",
) -> MagicMock:
    """构造 mock graph（astream_events + aget_state）"""
    mock_graph = MagicMock()
    mock_graph.astream_events = astream_events_fn
    mock_final = MagicMock()
    mock_final.values = {"final_response": final_response, "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)
    return mock_graph


_FIXTURE_STOCK_EVENTS: list[dict[str, object]] = [
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

_FIXTURE_GENERAL_EVENTS: list[dict[str, object]] = [
    {
        "event": "on_chat_model_stream",
        "name": "ChatOpenAI",
        "metadata": {"langgraph_node": "general_agent"},
        "data": {"chunk": _make_chunk("这是通用回复")},
    },
]


# ── /chat/stream/messages 测试 ──


@pytest.mark.asyncio
async def test_chat_stream_messages_content_type():
    """Content-Type 为 text/event-stream"""
    mock_graph = _make_mock_graph(_empty_stream)
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _MESSAGES_URL, json={"message": "你好"}, headers=_VALID_HEADERS,
            ) as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_chat_stream_messages_stock_intent_events():
    """stock 意图 messages 流：过滤 supervisor + tool 事件，仅 llm_start/text/done"""
    mock_graph = _make_mock_graph(
        _make_stream(_FIXTURE_STOCK_EVENTS),
        final_response="茅台分析完成",
    )
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _MESSAGES_URL, json={"message": "分析 600519"},
                headers=_VALID_HEADERS,
            ) as resp:
                text = await _read_sse(resp)
    events = _parse_sse(text)
    types = [e["type"] for e in events]
    # messages 流：不含 tool 事件（filter_type="text"）
    assert SSEEventType.TOOL_START not in types
    assert SSEEventType.TOOL_END not in types
    assert SSEEventType.LLM_START in types
    assert SSEEventType.TEXT in types
    assert events[-1]["type"] == SSEEventType.DONE
    # done 携带 final_response
    assert events[-1]["final_response"] == "茅台分析完成"
    # supervisor 事件被过滤：不应出现 intent JSON 文本
    text_contents = [e.get("content", "") for e in events if e["type"] == SSEEventType.TEXT]
    assert not any("intent" in c for c in text_contents)


@pytest.mark.asyncio
async def test_chat_stream_messages_general_intent_events():
    """general 意图 messages 流：只有 llm_start/text/done，无 tool_start"""
    mock_graph = _make_mock_graph(
        _make_stream(_FIXTURE_GENERAL_EVENTS),
        final_response="这是通用回复",
    )
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _MESSAGES_URL, json={"message": "你好"},
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
async def test_chat_stream_done_returns_advisor_trace():
    trace = {
        "schema_version": "advisor_trace.v1",
        "subquestions": [
            {"intent": "morning", "reports": [], "sources": [], "as_of": None,
             "missing_sources": [], "degraded": False},
            {"intent": "stock", "reports": [], "sources": [], "as_of": None,
             "missing_sources": ["stock_trace"], "degraded": True},
        ],
        "missing_sources": ["stock_trace"],
        "degraded": True,
    }
    mock_graph = _make_mock_graph(_empty_stream)
    mock_graph.aget_state = AsyncMock(return_value=MagicMock(values={
        "final_response": "降级",
        "analysis_reports": {},
        "advisor_trace": trace,
    }))
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            async with client.stream(
                "POST", _MESSAGES_URL, json={"message": "个股 600519"}, headers=_VALID_HEADERS
            ) as resp:
                events = _parse_sse(await _read_sse(resp))

    assert events[-1]["advisor_trace"] == trace


@pytest.mark.asyncio
async def test_chat_stream_messages_error_event():
    """astream_events 抛异常 → SSE error 事件"""
    mock_graph = _make_mock_graph(_boom_stream)
    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream(
                "POST", _MESSAGES_URL, json={"message": "你好"},
                headers=_VALID_HEADERS,
            ) as resp:
                text = await _read_sse(resp)
    events = _parse_sse(text)
    assert any(e["type"] == SSEEventType.ERROR for e in events)


@pytest.mark.asyncio
async def test_chat_stream_messages_missing_token_403():
    """缺失 X-Internal-Token → 403"""
    with patch("aistock_agent.api.routes.compile_graph",
               side_effect=AssertionError("auth should block")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(_MESSAGES_URL, json={"message": "你好"})
    assert resp.status_code == 403


# ── /chat/stream/updates 测试 ──


@pytest.mark.asyncio
async def test_chat_stream_updates_tool_events():
    """updates 流：仅 TOOL_START/END + AGENT_SWITCH + DONE，无 TEXT"""
    # updates 流只读 update queue（不触发 graph），需预填充事件
    from aistock_agent.api import routes as routes_mod

    session_id = "test-updates-e2e"
    routes_mod._message_queues.pop(session_id, None)
    routes_mod._update_queues.pop(session_id, None)

    # 预填充 update queue（模拟 messages 流已触发 graph 执行后的事件副本）
    queue = routes_mod._ensure_update_queue(session_id)
    for event in _FIXTURE_STOCK_EVENTS:
        await queue.put(event)
    await queue.put(None)  # 哨兵

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        async with client.stream(
            "POST", _UPDATES_URL,
            json={"message": "分析 600519", "session_id": session_id},
            headers=_VALID_HEADERS,
        ) as resp:
            text = await _read_sse(resp)

    events = _parse_sse(text)
    types = [e["type"] for e in events]
    # updates 流：不含 text 事件（filter_type="tool"）
    assert SSEEventType.TEXT not in types
    assert SSEEventType.TOOL_START in types
    assert SSEEventType.TOOL_END in types
    assert SSEEventType.AGENT_SWITCH in types
    assert types[-1] == SSEEventType.DONE

    routes_mod._message_queues.pop(session_id, None)
    routes_mod._update_queues.pop(session_id, None)


@pytest.mark.asyncio
async def test_chat_stream_updates_missing_token_403():
    """updates 流缺失 X-Internal-Token → 403"""
    with patch("aistock_agent.api.routes.compile_graph",
               side_effect=AssertionError("auth should block")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(_UPDATES_URL, json={"message": "你好"})
    assert resp.status_code == 403
