"""RedisPool 单例测试 — lifespan 管理的 Redis 连接池

验证：
- init 后 get_client 返回 Redis 实例
- 未 init 时 get_client 抛 RuntimeError
- close 后状态重置
- 重复 init 幂等
- 未 init 时 close 安全
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.redis_pool import RedisPool


@pytest.fixture(autouse=True)
def _reset_redis_pool():
    """每个测试前重置 RedisPool 类级状态"""
    RedisPool._pool = None
    RedisPool._client = None
    yield
    RedisPool._pool = None
    RedisPool._client = None


@pytest.mark.asyncio
async def test_init_creates_pool_and_client():
    """init 后 get_client 返回 Redis 实例"""
    with patch("aistock_agent.services.redis_pool.aioredis.ConnectionPool") as mock_pool_cls, \
         patch("aistock_agent.services.redis_pool.aioredis.Redis") as mock_redis_cls:
        mock_pool = AsyncMock()
        mock_client = AsyncMock()
        mock_pool_cls.from_url.return_value = mock_pool
        mock_redis_cls.return_value = mock_client

        await RedisPool.init("redis://localhost:6379/1")
        client = await RedisPool.get_client()

        assert client is mock_client
        mock_pool_cls.from_url.assert_called_once()


@pytest.mark.asyncio
async def test_init_passes_max_connections():
    """init 将 max_connections 传给 ConnectionPool.from_url"""
    with patch("aistock_agent.services.redis_pool.aioredis.ConnectionPool") as mock_pool_cls, \
         patch("aistock_agent.services.redis_pool.aioredis.Redis"):
        mock_pool_cls.from_url.return_value = AsyncMock()

        await RedisPool.init("redis://localhost:6379/1", max_connections=50)

        mock_pool_cls.from_url.assert_called_once_with(
            "redis://localhost:6379/1", max_connections=50,
        )


@pytest.mark.asyncio
async def test_get_client_before_init_raises():
    """未 init 时 get_client 抛 RuntimeError"""
    with pytest.raises(RuntimeError, match="not initialized"):
        await RedisPool.get_client()


@pytest.mark.asyncio
async def test_close_resets_state():
    """close 后 get_client 抛 RuntimeError"""
    with patch("aistock_agent.services.redis_pool.aioredis.ConnectionPool") as mock_pool_cls, \
         patch("aistock_agent.services.redis_pool.aioredis.Redis") as mock_redis_cls:
        mock_client = AsyncMock()
        mock_pool = AsyncMock()
        mock_pool_cls.from_url.return_value = mock_pool
        mock_redis_cls.return_value = mock_client

        await RedisPool.init("redis://localhost:6379/1")
        await RedisPool.close()

        mock_client.aclose.assert_awaited_once()
        mock_pool.aclose.assert_awaited_once()

    with pytest.raises(RuntimeError, match="not initialized"):
        await RedisPool.get_client()


@pytest.mark.asyncio
async def test_double_init_is_idempotent():
    """重复 init 不创建新连接池"""
    with patch("aistock_agent.services.redis_pool.aioredis.ConnectionPool") as mock_pool_cls, \
         patch("aistock_agent.services.redis_pool.aioredis.Redis") as mock_redis_cls:
        mock_pool_cls.from_url.return_value = AsyncMock()
        mock_redis_cls.return_value = AsyncMock()

        await RedisPool.init("redis://localhost:6379/1")
        await RedisPool.init("redis://localhost:6379/1")

        mock_pool_cls.from_url.assert_called_once()
        mock_redis_cls.assert_called_once()


@pytest.mark.asyncio
async def test_close_when_not_initialized_is_safe():
    """未 init 时 close 不抛异常"""
    await RedisPool.close()  # should not raise


@pytest.mark.asyncio
async def test_reinit_after_close():
    """close 后可重新 init"""
    with patch("aistock_agent.services.redis_pool.aioredis.ConnectionPool") as mock_pool_cls, \
         patch("aistock_agent.services.redis_pool.aioredis.Redis") as mock_redis_cls:
        mock_pool_cls.from_url.return_value = AsyncMock()
        mock_client = AsyncMock()
        mock_redis_cls.return_value = mock_client

        await RedisPool.init("redis://localhost:6379/1")
        await RedisPool.close()
        await RedisPool.init("redis://localhost:6379/1")
        client = await RedisPool.get_client()

        assert client is mock_client
        assert mock_pool_cls.from_url.call_count == 2
