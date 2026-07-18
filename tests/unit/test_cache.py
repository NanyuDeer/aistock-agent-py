"""services/cache.py 缓存服务测试

验证从 Phase 4 内联 aioredis.from_url 迁移到 RedisPool 后：
- 缓存命中/未命中
- bytes / str 返回值处理
- Redis 异常时降级返回 None（不崩溃）
- set_cached_briefing 正确写入 key + TTL
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services import cache


@pytest.mark.asyncio
async def test_get_cached_briefing_hit():
    """缓存命中：返回解码后的字符串"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=b"cached content")

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        result = await cache.get_cached_briefing()

    assert result == "cached content"
    today = datetime.now().strftime("%Y-%m-%d")
    mock_client.get.assert_awaited_once_with(f"briefing:morning:{today}")


@pytest.mark.asyncio
async def test_get_cached_briefing_miss():
    """缓存未命中：返回 None"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=None)

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        result = await cache.get_cached_briefing()

    assert result is None


@pytest.mark.asyncio
async def test_get_cached_briefing_string_value():
    """缓存值为字符串（非 bytes）时转为 str 返回"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value="string content")

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        result = await cache.get_cached_briefing()

    assert result == "string content"


@pytest.mark.asyncio
async def test_get_cached_briefing_empty_bytes_returns_none():
    """空 bytes 视为 falsy，返回 None"""
    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=b"")

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        result = await cache.get_cached_briefing()

    assert result is None


@pytest.mark.asyncio
async def test_get_cached_briefing_error_returns_none():
    """Redis 异常时返回 None（不崩溃）"""
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(side_effect=RuntimeError("pool not init"))
        result = await cache.get_cached_briefing()

    assert result is None


@pytest.mark.asyncio
async def test_set_cached_briefing_writes():
    """缓存写入：调用 setex with correct key and TTL=86400（每日更新语义）"""
    mock_client = AsyncMock()
    mock_client.setex = AsyncMock()

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.set_cached_briefing("briefing content")

    today = datetime.now().strftime("%Y-%m-%d")
    mock_client.setex.assert_awaited_once_with(
        f"briefing:morning:{today}", 86400, "briefing content",
    )


@pytest.mark.asyncio
async def test_set_cached_review_writes():
    """复盘缓存写入：key=briefing:review:{date}，TTL=86400（每日更新语义）"""
    mock_client = AsyncMock()
    mock_client.setex = AsyncMock()

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.set_cached_review("review content")

    today = datetime.now().strftime("%Y-%m-%d")
    mock_client.setex.assert_awaited_once_with(
        f"briefing:review:{today}", 86400, "review content",
    )


@pytest.mark.asyncio
async def test_set_cached_briefing_custom_ttl():
    """自定义 TTL"""
    mock_client = AsyncMock()
    mock_client.setex = AsyncMock()

    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.set_cached_briefing("content", ttl=3600)

    today = datetime.now().strftime("%Y-%m-%d")
    mock_client.setex.assert_awaited_once_with(
        f"briefing:morning:{today}", 3600, "content",
    )


@pytest.mark.asyncio
async def test_set_cached_briefing_error_silent():
    """Redis 异常时不崩溃"""
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(side_effect=RuntimeError("pool not init"))
        await cache.set_cached_briefing("content")  # should not raise
