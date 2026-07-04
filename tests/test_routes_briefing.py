"""routes /briefing/morning SSE 端点测试"""
import json
import pytest
import httpx
from unittest.mock import patch

from aistock_agent.main import app


async def _mock_stream_ok(state):
    yield {"type": "tool_start", "tool": "get_global_markets",
           "label": "正在获取全球市场行情"}
    yield {"type": "tool_end", "tool": "get_global_markets"}
    yield {"type": "llm_start", "label": "正在生成分析报告"}
    yield {"type": "text", "content": "今日晨报内容"}
    yield {"type": "done"}


async def _mock_stream_error(state):
    yield {"type": "error", "message": "LLM unavailable"}


@pytest.mark.asyncio
async def test_briefing_morning_content_type():
    """响应 Content-Type 必须是 text/event-stream"""
    with patch("aistock_agent.api.routes.morning_agent.stream",
               side_effect=_mock_stream_ok):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", "/api/agent/briefing/morning") as resp:
                assert resp.status_code == 200
                assert "text/event-stream" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_briefing_morning_sse_events():
    """SSE 数据行可解析为预期 JSON 事件序列"""
    with patch("aistock_agent.api.routes.morning_agent.stream",
               side_effect=_mock_stream_ok):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", "/api/agent/briefing/morning") as resp:
                data_events = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_events.append(json.loads(line[5:].strip()))

    types = [e["type"] for e in data_events]
    assert "tool_start" in types
    assert "text" in types
    assert types[-1] == "done"

    text_event = next(e for e in data_events if e["type"] == "text")
    assert text_event["content"] == "今日晨报内容"


@pytest.mark.asyncio
async def test_briefing_morning_error_event():
    """stream 内部 yield error 时，SSE 正确传递 error 事件"""
    with patch("aistock_agent.api.routes.morning_agent.stream",
               side_effect=_mock_stream_error):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            async with client.stream("GET", "/api/agent/briefing/morning") as resp:
                data_events = []
                async for line in resp.aiter_lines():
                    if line.startswith("data:"):
                        data_events.append(json.loads(line[5:].strip()))

    assert data_events[0] == {"type": "error", "message": "LLM unavailable"}
