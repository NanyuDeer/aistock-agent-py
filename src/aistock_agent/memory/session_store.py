"""会话消息历史存储 — 基于 Redis

key 格式：``session:{session_id}:messages``
序列化：JSON（list[dict]）

与 ``agents/workers/morning.py`` 的 Redis 用法一致：每次 ``aioredis.from_url``
取得客户端，用完 ``aclose``（Phase 5 改 lifespan 池后统一复用）。
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis

from aistock_agent.config import settings


async def save_session(session_id: str, messages: list[dict[str, object]]) -> None:
    """保存会话消息历史到 Redis。"""
    client = aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    try:
        key = f"session:{session_id}:messages"
        await client.set(key, json.dumps(messages, ensure_ascii=False))
    finally:
        await client.aclose()


async def load_session(session_id: str) -> list[dict[str, object]]:
    """加载会话消息历史，不存在返回空 list。"""
    client = aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    try:
        key = f"session:{session_id}:messages"
        data = await client.get(key)
        if data is None:
            return []
        raw = data.decode() if isinstance(data, bytes) else data
        return json.loads(raw)  # type: ignore[no-any-return]
    finally:
        await client.aclose()
