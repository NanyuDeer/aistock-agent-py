"""routes /chat/message 端到端测试 — M5 入口路由切换后

M5 后 /chat/message 恒走 ChatAgent（compile_chat_graph），不再走老路径。本文件验证：
- 恒走 compile_chat_graph，返回 content（不含已退役字段）
- 澄清路径 content 透出
- 空 message 被 Pydantic 拦截（不触达 graph）

老路径意图路由由 tests/integration/test_graph.py 覆盖；
鉴权契约由 tests/e2e/test_chat_message_auth.py 覆盖。
"""
from unittest.mock import MagicMock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.main import app

_CHAT_URL = "/api/agent/chat/message"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}


def _mock_chat_graph(final_response: str, token_usage: dict | None = None) -> MagicMock:
    """mock compile_chat_graph 返回的 graph（ainvoke 返回固定 final_response，可选 token_usage）。"""
    async def mock_ainvoke(state, config=None):
        result: dict = {
            "final_response": final_response,
            "insight": None,
            "trace": None,
        }
        if token_usage is not None:
            result["token_usage"] = token_usage
        return result

    mock_graph = MagicMock()
    mock_graph.ainvoke = mock_ainvoke
    return mock_graph


@pytest.mark.asyncio
async def test_chat_message_returns_content_without_trace_field():
    """/chat/message 恒走 ChatAgent：content 透出、响应不含该字段。"""
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=_mock_chat_graph("ChatAgent 回复"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "茅台今天怎么样"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "ChatAgent 回复"
    assert "advisor_trace" not in body
    assert "session_id" in body


@pytest.mark.asyncio
async def test_chat_message_returns_token_usage_when_graph_provides():
    """/chat/message HTTP 降级路径透出 token_usage（P10 线 2 缺口修复：前端降级分支需读取）"""
    usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=_mock_chat_graph("ChatAgent 回复", token_usage=usage),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "茅台今天怎么样"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["token_usage"] == usage
    assert body["token_usage"]["total_tokens"] == 30


@pytest.mark.asyncio
async def test_chat_message_clarification_content():
    """澄清路径（个股缺代码）content 透出，响应不含该字段。"""
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        return_value=_mock_chat_graph("请提供 6 位股票代码后重试。"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "茅台最近新闻"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "请提供 6 位股票代码后重试。"
    assert "advisor_trace" not in body


@pytest.mark.asyncio
async def test_chat_message_empty_message_returns_422():
    """空 message 被 Pydantic min_length=1 拦截，返回 422（不触达 compile_chat_graph）。"""
    with patch(
        "aistock_agent.api.routes.compile_chat_graph",
        side_effect=AssertionError("Pydantic should block before compile_chat_graph"),
    ):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": ""},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 422
