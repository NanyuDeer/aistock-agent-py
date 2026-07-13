"""Redis 缓存服务 — 基于 RedisPool 单例

从 Phase 4 各模块内联的 ``aioredis.from_url()`` 迁移到 lifespan 管理的
连接池，消除每次请求创建/销毁连接的开销。

提供晨报/复盘/事件缓存（get/set）。
"""

from __future__ import annotations

import hashlib
import json
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


async def get_cached_review() -> str | None:
    """从 Redis 获取缓存复盘报告。

    缓存 key 格式：``briefing:review:{YYYY-MM-DD}``

    Returns:
        缓存的复盘文本，未命中或异常时返回 None。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:review:{today}"
        cached = await client.get(cache_key)
        if cached:
            if isinstance(cached, bytes):
                return cached.decode()
            return str(cached)
    except Exception:
        logger.debug("get_cached_review_failed", exc_info=True)
    return None


async def set_cached_review(content: str, ttl: int = 7200) -> None:
    """缓存复盘报告到 Redis。

    Args:
        content: 复盘文本。
        ttl: 缓存过期秒数，默认 7200（2 小时）。
    """
    try:
        client = await RedisPool.get_client()
        today = datetime.now().strftime("%Y-%m-%d")
        cache_key = f"briefing:review:{today}"
        await client.setex(cache_key, ttl, content)
    except Exception:
        logger.debug("set_cached_review_failed", exc_info=True)


def _event_cache_key(user_input: str) -> str:
    """生成事件缓存 key：event:{md5}"""
    digest = hashlib.md5(user_input.encode()).hexdigest()
    return f"event:{digest}"


async def get_cached_event(user_input: str) -> dict[str, object] | None:
    """从 Redis 获取缓存的事件分析结果（完整 analysis_reports）。

    缓存 key 基于事件内容 MD5，TTL 30 分钟（写入时设定）。
    与晨报/复盘不同，事件缓存是 struct 而非纯文本。

    缓存存储的是完整的 ``analysis_reports`` dict（transform_to_frontend 的输出 +
    event_podcast_brief），保证缓存命中时前端数据结构与新鲜执行一致。

    Args:
        user_input: 用户输入的事件描述文本。

    Returns:
        缓存的 analysis_reports dict，未命中或异常返回 None。
    """
    try:
        client = await RedisPool.get_client()
        key = _event_cache_key(user_input)
        cached = await client.get(key)
        if cached:
            raw = cached.decode() if isinstance(cached, bytes) else str(cached)
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
    except Exception:
        logger.debug("event_cache_check_failed", exc_info=True)
    return None


async def set_cached_event(
    user_input: str,
    analysis_reports: dict[str, object],
    ttl: int = 1800,
) -> None:
    """缓存事件分析结果到 Redis（完整 analysis_reports）。

    缓存存储的是完整的 ``analysis_reports`` dict（transform_to_frontend 的输出 +
    event_podcast_brief），保证缓存命中时前端数据结构与新鲜执行一致。

    Args:
        user_input: 用户输入的事件描述文本（用于生成 MD5 key）。
        analysis_reports: 完整的前端对齐 analysis_reports dict。
        ttl: 缓存过期秒数，默认 1800（30 分钟）。
    """
    try:
        client = await RedisPool.get_client()
        key = _event_cache_key(user_input)
        value = json.dumps(analysis_reports, ensure_ascii=False)
        await client.setex(key, ttl, value)
    except Exception:
        logger.debug("event_cache_set_failed", exc_info=True)
