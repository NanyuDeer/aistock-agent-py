"""HttpClientPool 单例测试 — lifespan 管理的 httpx.AsyncClient

验证：
- init 后 get_client 返回 AsyncClient 实例
- 默认超时 10s，可自定义
- 未 init 时 get_client 抛 RuntimeError
- close 后状态重置
- 重复 init 幂等
- 未 init 时 close 安全
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.http_client import HttpClientPool


@pytest.fixture(autouse=True)
def _reset_http_client_pool():
    """每个测试前重置 HttpClientPool 类级状态"""
    HttpClientPool._client = None
    yield
    HttpClientPool._client = None


@pytest.mark.asyncio
async def test_init_creates_client():
    """init 后 get_client 返回 AsyncClient 实例"""
    with patch("aistock_agent.services.http_client.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value = mock_client

        await HttpClientPool.init()
        client = await HttpClientPool.get_client()

        assert client is mock_client
        mock_cls.assert_called_once()


@pytest.mark.asyncio
async def test_init_default_timeout_10s():
    """默认超时 10s"""
    with patch("aistock_agent.services.http_client.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = AsyncMock()

        await HttpClientPool.init()

        mock_cls.assert_called_once_with(timeout=10.0)


@pytest.mark.asyncio
async def test_init_custom_timeout():
    """自定义超时"""
    with patch("aistock_agent.services.http_client.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = AsyncMock()

        await HttpClientPool.init(timeout=30.0)

        mock_cls.assert_called_once_with(timeout=30.0)


@pytest.mark.asyncio
async def test_get_client_before_init_raises():
    """未 init 时 get_client 抛 RuntimeError"""
    with pytest.raises(RuntimeError, match="not initialized"):
        await HttpClientPool.get_client()


@pytest.mark.asyncio
async def test_close_resets_state():
    """close 后 get_client 抛 RuntimeError"""
    with patch("aistock_agent.services.http_client.httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_cls.return_value = mock_client

        await HttpClientPool.init()
        await HttpClientPool.close()

        mock_client.aclose.assert_awaited_once()

    with pytest.raises(RuntimeError, match="not initialized"):
        await HttpClientPool.get_client()


@pytest.mark.asyncio
async def test_double_init_is_idempotent():
    """重复 init 不创建新 client"""
    with patch("aistock_agent.services.http_client.httpx.AsyncClient") as mock_cls:
        mock_cls.return_value = AsyncMock()

        await HttpClientPool.init()
        await HttpClientPool.init()

        mock_cls.assert_called_once()


@pytest.mark.asyncio
async def test_close_when_not_initialized_is_safe():
    """未 init 时 close 不抛异常"""
    await HttpClientPool.close()  # should not raise
