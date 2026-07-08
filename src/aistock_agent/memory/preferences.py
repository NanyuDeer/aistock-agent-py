"""用户自选股偏好 — 基于 Redis

key 格式：``user:{user_id}:favorites``
序列化：JSON（list[str]）

直接用 ``aioredis.from_url(settings.redis_url)``（与 morning.py / api.deps 一致）；
services/cache.py 尚未抽出（见 controller decision 2）。
"""

from __future__ import annotations

import json

import redis.asyncio as aioredis

from aistock_agent.config import settings


async def get_user_favorites(user_id: str) -> list[str]:
    """获取用户自选股列表，不存在返回空 list。"""
    client = aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    try:
        key = f"user:{user_id}:favorites"
        data = await client.get(key)
        if data is None:
            return []
        raw = data.decode() if isinstance(data, bytes) else data
        return json.loads(raw)  # type: ignore[no-any-return]
    finally:
        await client.aclose()


async def set_user_favorites(user_id: str, symbols: list[str]) -> None:
    """保存用户自选股列表到 Redis。"""
    client = aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call]
    try:
        key = f"user:{user_id}:favorites"
        await client.set(key, json.dumps(symbols, ensure_ascii=False))
    finally:
        await client.aclose()
