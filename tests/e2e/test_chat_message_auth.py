"""routes /chat/message 鉴权测试 — X-Internal-Token 校验

验证 Task 4 抽离的 ``api.deps.verify_internal_token`` 经 FastAPI ``Depends``
注入到 /chat/message 后，鉴权行为正确：
- 缺失 token → 403
- 错误 token → 403
- 正确 token → 通过鉴权（mock graph 避免真实 LLM 调用）

测试风格与 ``tests/e2e/test_briefing_morning.py`` 一致，使用
``httpx.AsyncClient`` + ``ASGITransport``。
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from aistock_agent.config import settings
from aistock_agent.main import app

_CHAT_URL = "/api/agent/chat/message"
_VALID_HEADERS = {"X-Internal-Token": settings.internal_api_token}


@pytest.mark.asyncio
async def test_chat_message_missing_token_returns_403():
    """缺失 X-Internal-Token 时返回 403（鉴权在业务逻辑前拦截）"""
    # compile_graph 不应被调用；若被调用说明鉴权未生效，让其显式报错
    with patch("aistock_agent.api.routes.compile_graph",
               side_effect=AssertionError("auth should block before compile_graph")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(_CHAT_URL, json={"message": "你好"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_message_invalid_token_returns_403():
    """X-Internal-Token 不匹配时返回 403"""
    with patch("aistock_agent.api.routes.compile_graph",
               side_effect=AssertionError("auth should block before compile_graph")):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "你好"},
                headers={"X-Internal-Token": "wrong-token"},
            )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_chat_message_valid_token_passes_auth():
    """正确 X-Internal-Token 通过鉴权，返回 200（mock graph 避免真实 LLM 调用）"""
    mock_graph = AsyncMock()
    mock_graph.ainvoke = AsyncMock(return_value={"final_response": "mocked 回复"})

    with patch("aistock_agent.api.routes.compile_graph",
               return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            resp = await client.post(
                _CHAT_URL,
                json={"message": "你好"},
                headers=_VALID_HEADERS,
            )
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "mocked 回复"
    assert "session_id" in body


@pytest.mark.asyncio
async def test_chat_message_returns_advisor_trace():
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
    mock_graph = AsyncMock()
    mock_graph.ainvoke = AsyncMock(return_value={"final_response": "降级", "advisor_trace": trace})

    with patch("aistock_agent.api.routes.compile_graph", return_value=mock_graph):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                _CHAT_URL, json={"message": "个股 600519"}, headers=_VALID_HEADERS
            )

    assert resp.json()["advisor_trace"] == trace
