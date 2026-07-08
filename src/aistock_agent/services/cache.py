"""Redis 缓存服务 — 基于 RedisPool 单例

从 Phase 4 各模块内联的 ``aioredis.from_url()`` 迁移到 lifespan 管理的
连接池，消除每次请求创建/销毁连接的开销。

目前提供晨报缓存（get/set），后续可扩展为通用缓存接口。
"""

from __future__ import annotations

from datetime import datetime

import structlog

from aistock_agent.services.redis_pool import RedisPool

logger = structlog.get_logger()


async def get_cached_briefing() -> str | None:
    """从 Redis 获取缓存晨报。

    缓存 key 格式：``briefing:morning:{YYYY-MM-DD}``

    Returns:
        缓存的晨报文本，未命中或异常时返回 None。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:morning:{today}"
        cached = await client.get(cache_key)
        if cached:
            if isinstance(cached, bytes):
                return cached.decode()
            return str(cached)
    except Exception:
        logger.debug("get_cached_briefing_failed", exc_info=True)
    return None


async def set_cached_briefing(content: str, ttl: int = 7200) -> None:
    """缓存晨报到 Redis。

    Args:
        content: 晨报文本。
        ttl: 缓存过期秒数，默认 7200（2 小时）。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:morning:{today}"
        await client.setex(cache_key, ttl, content)
    except Exception:
        logger.debug("set_cached_briefing_failed", exc_info=True)
