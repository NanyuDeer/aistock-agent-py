"""data_client.get_user_profile 测试（Phase 4-3 Task 3）。

覆盖：成功拉取 / Redis 缓存命中 / 拉取失败降级 None / 空画像 {} 缓存。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import NodeApiClient


@pytest.fixture
def client() -> NodeApiClient:
    return NodeApiClient()


def _redis_client(cached_value: bytes | None = None) -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=cached_value)
    redis.setex = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_get_user_profile_success(client: NodeApiClient):
    """拉取成功 → 返回 profile 并写入缓存。"""
    profile = {
        "user_id": "o_1",
        "nickname": "小王",
        "investment_preferences": ["白酒"],
        "risk_tolerance": "conservative",
    }
    redis = _redis_client()
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(
            client,
            "get",
            new=AsyncMock(return_value=profile),
        ) as get,
    ):
        result = await client.get_user_profile("o_1")

    assert result == profile
    get.assert_awaited_once_with("/internal/user-profile/o_1")
    redis.setex.assert_awaited_once()
    cache_key = redis.setex.await_args.args[0]
    assert cache_key == "user_profile:o_1"
    assert redis.setex.await_args.args[1] == 300  # TTL 5min


@pytest.mark.asyncio
async def test_get_user_profile_cache_hit(client: NodeApiClient):
    """Redis 命中 → 直接返回缓存，不发 HTTP 请求。"""
    cached = json.dumps({"user_id": "o_1", "nickname": "老张"}, ensure_ascii=False).encode()
    redis = _redis_client(cached_value=cached)
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(client, "get", new=AsyncMock()) as get,
    ):
        result = await client.get_user_profile("o_1")

    assert result == {"user_id": "o_1", "nickname": "老张"}
    get.assert_not_awaited()
    redis.setex.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_user_profile_failure_degrades_to_none(client: NodeApiClient):
    """拉取失败（HTTP 异常 → get 返回 None）→ warning 降级 None，不阻断。"""
    redis = _redis_client()
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(client, "get", new=AsyncMock(return_value=None)) as get,
    ):
        result = await client.get_user_profile("o_missing")

    assert result is None
    get.assert_awaited_once()
    redis.setex.assert_not_awaited()  # 失败不写缓存


@pytest.mark.asyncio
async def test_get_user_profile_empty_profile_cached(client: NodeApiClient):
    """空画像（data={}）→ 返回 {}（区别于失败 None）并缓存，避免每轮重复拉取。"""
    redis = _redis_client()
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(client, "get", new=AsyncMock(return_value={})),
    ):
        result = await client.get_user_profile("o_none")

    assert result == {}
    assert redis.setex.await_args.args[0] == "user_profile:o_none"
