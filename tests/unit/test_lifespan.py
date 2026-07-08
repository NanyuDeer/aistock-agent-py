"""main.lifespan 生命周期测试

验证 lifespan：
- startup 初始化 RedisPool + HttpClientPool
- shutdown 关闭 RedisPool + HttpClientPool
- Redis init 失败时 app 不崩溃，HTTP client 仍初始化，shutdown 仍关闭
- HTTP init 失败时 app 不崩溃，shutdown 仍关闭
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI

from aistock_agent.config import settings
from aistock_agent.main import lifespan


@pytest.mark.asyncio
async def test_lifespan_initializes_pools_on_startup():
    """lifespan startup 初始化 RedisPool 和 HttpClientPool"""
    with patch("aistock_agent.main.RedisPool") as mock_redis, \
         patch("aistock_agent.main.HttpClientPool") as mock_http:
        mock_redis.init = AsyncMock()
        mock_http.init = AsyncMock()
        mock_redis.close = AsyncMock()
        mock_http.close = AsyncMock()

        app = FastAPI()
        async with lifespan(app):
            mock_redis.init.assert_awaited_once()
            mock_http.init.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_closes_pools_on_shutdown():
    """lifespan shutdown 关闭 RedisPool 和 HttpClientPool"""
    with patch("aistock_agent.main.RedisPool") as mock_redis, \
         patch("aistock_agent.main.HttpClientPool") as mock_http:
        mock_redis.init = AsyncMock()
        mock_http.init = AsyncMock()
        mock_redis.close = AsyncMock()
        mock_http.close = AsyncMock()

        app = FastAPI()
        async with lifespan(app):
            pass  # startup done, now shutdown

        mock_redis.close.assert_awaited_once()
        mock_http.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_redis_init_failure_does_not_crash():
    """Redis init 失败时 app 不崩溃，HTTP client 仍初始化"""
    with patch("aistock_agent.main.RedisPool") as mock_redis, \
         patch("aistock_agent.main.HttpClientPool") as mock_http:
        mock_redis.init = AsyncMock(side_effect=RuntimeError("redis down"))
        mock_http.init = AsyncMock()
        mock_redis.close = AsyncMock()
        mock_http.close = AsyncMock()

        app = FastAPI()
        # Should not raise
        async with lifespan(app):
            mock_http.init.assert_awaited_once()

        # Shutdown still closes both pools
        mock_redis.close.assert_awaited_once()
        mock_http.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_http_init_failure_does_not_crash():
    """HTTP client init 失败时 app 不崩溃"""
    with patch("aistock_agent.main.RedisPool") as mock_redis, \
         patch("aistock_agent.main.HttpClientPool") as mock_http:
        mock_redis.init = AsyncMock()
        mock_http.init = AsyncMock(side_effect=RuntimeError("httpx error"))
        mock_redis.close = AsyncMock()
        mock_http.close = AsyncMock()

        app = FastAPI()
        # Should not raise
        async with lifespan(app):
            mock_redis.init.assert_awaited_once()

        mock_redis.close.assert_awaited_once()
        mock_http.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_passes_config_to_pools():
    """lifespan 传递 config 参数到 RedisPool.init 和 HttpClientPool.init"""
    with patch("aistock_agent.main.RedisPool") as mock_redis, \
         patch("aistock_agent.main.HttpClientPool") as mock_http:
        mock_redis.init = AsyncMock()
        mock_http.init = AsyncMock()
        mock_redis.close = AsyncMock()
        mock_http.close = AsyncMock()

        app = FastAPI()
        async with lifespan(app):
            mock_redis.init.assert_awaited_once_with(
                settings.redis_url,
                max_connections=settings.redis_max_connections,
            )
            mock_http.init.assert_awaited_once_with(
                timeout=settings.http_timeout_seconds,
            )
