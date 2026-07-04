"""pytest 配置 — 共享 fixtures"""

import pytest
from unittest.mock import AsyncMock, patch


@pytest.fixture(autouse=True)
def _reset_sse_appstatus():
    """每个测试前重置 sse-starlette 的类级 AppStatus。

    AppStatus.should_exit_event 是类级单例，首个 SSE 响应会创建绑定到当前事件循环的
    anyio.Event；后续测试在新事件循环上复用会触发 "bound to a different event loop"。
    """
    from sse_starlette.sse import AppStatus
    AppStatus.should_exit = False
    AppStatus.should_exit_event = None
    yield


@pytest.fixture
def mock_node_api():
    """mock NodeApiClient.get，返回预设数据"""
    with patch("aistock_agent.services.data_client.NodeApiClient") as mock_cls:
        instance = mock_cls.return_value
        instance.get = AsyncMock()
        yield instance.get


@pytest.fixture
def mock_yfinance():
    """mock yfinance 数据"""
    with patch("aistock_agent.tools.market_tools.yf") as mock_yf:
        yield mock_yf


@pytest.fixture
def mock_tavily():
    """mock TavilyClient"""
    with patch("aistock_agent.tools.market_tools.TavilyClient") as mock_cls:
        yield mock_cls


@pytest.fixture
def mock_redis():
    """mock redis.asyncio"""
    with patch("aistock_agent.agents.morning_agent.aioredis") as mock_aioredis:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=None)
        mock_client.setex = AsyncMock()
        mock_client.aclose = AsyncMock()
        mock_aioredis.from_url.return_value = mock_client
        yield mock_client
