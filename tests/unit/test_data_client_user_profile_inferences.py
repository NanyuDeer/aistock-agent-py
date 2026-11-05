"""data_client.get_user_profile_inferences 测试（用户画像自动挖掘一期 Task 6）。

覆盖：成功拉取 / Redis 缓存命中 / 拉取失败降级 None（不写缓存）/
空数组 [] 与 None 区分（[] 视为确认无推断并缓存）/ 缓存脏数据回退 HTTP。
"""
import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import NodeApiClient

INFERENCES = [
    {
        "dimension": "preferred_sectors",
        "value": {
            "items": [{"name": "白酒", "stock_count": 3, "weight": 0.19}],
            "total": 16,
        },
        "confidence": 0.7,
        "sample_size": 16,
        "window_days": None,
        "inferred_at": "2026-11-05T00:30:00Z",
    },
    {
        "dimension": "question_topics",
        "value": {"items": [{"topic": "财报", "hits": 12}], "total": 40},
        "confidence": 0.65,
        "sample_size": 40,
        "window_days": 30,
        "inferred_at": "2026-11-05T00:30:00Z",
    },
]


@pytest.fixture
def client() -> NodeApiClient:
    return NodeApiClient()


def _redis_client(cached_value: bytes | None = None) -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=cached_value)
    redis.setex = AsyncMock()
    return redis


@pytest.mark.asyncio
async def test_get_user_profile_inferences_success(client: NodeApiClient):
    """拉取成功 → 返回推断列表并写入缓存（key/TTL 与 user_profile 对齐）。"""
    redis = _redis_client()
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(
            client,
            "get_list",
            new=AsyncMock(return_value=INFERENCES),
        ) as get_list,
    ):
        result = await client.get_user_profile_inferences("o_1")

    assert result == INFERENCES
    get_list.assert_awaited_once_with("/internal/user-profile-inferences/o_1")
    redis.setex.assert_awaited_once()
    assert redis.setex.await_args.args[0] == "user_profile_inferences:o_1"
    assert redis.setex.await_args.args[1] == 300  # TTL 5min


@pytest.mark.asyncio
async def test_get_user_profile_inferences_cache_hit(client: NodeApiClient):
    """Redis 命中 → 直接返回缓存列表，不发 HTTP 请求。"""
    cached = json.dumps(INFERENCES, ensure_ascii=False).encode()
    redis = _redis_client(cached_value=cached)
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(client, "get_list", new=AsyncMock()) as get_list,
    ):
        result = await client.get_user_profile_inferences("o_1")

    assert result == INFERENCES
    get_list.assert_not_awaited()
    redis.setex.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_user_profile_inferences_failure_degrades_to_none(
    client: NodeApiClient,
):
    """拉取失败（HTTP 异常 / 非列表 → get_list 返回 None）→ None，且不写缓存。"""
    redis = _redis_client()
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(client, "get_list", new=AsyncMock(return_value=None)) as get_list,
    ):
        result = await client.get_user_profile_inferences("o_missing")

    assert result is None
    get_list.assert_awaited_once()
    redis.setex.assert_not_awaited()  # 失败不写缓存（避免把故障固化 5min）


@pytest.mark.asyncio
async def test_get_user_profile_inferences_empty_list_cached(client: NodeApiClient):
    """空数组 [] = 确认无推断 → 返回 []（区别于失败 None）并缓存。"""
    redis = _redis_client()
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(client, "get_list", new=AsyncMock(return_value=[])),
    ):
        result = await client.get_user_profile_inferences("o_none")

    assert result == []
    assert result is not None
    assert redis.setex.await_args.args[0] == "user_profile_inferences:o_none"
    assert json.loads(redis.setex.await_args.args[2]) == []


@pytest.mark.asyncio
async def test_get_user_profile_inferences_corrupt_cache_falls_back(
    client: NodeApiClient,
):
    """缓存为脏数据（非 JSON / 非列表）→ 忽略缓存，回退 HTTP 拉取。"""
    redis = _redis_client(cached_value=b'{"not": "a list"}')
    with (
        patch(
            "aistock_agent.services.data_client.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
        patch.object(
            client, "get_list", new=AsyncMock(return_value=INFERENCES)
        ) as get_list,
    ):
        result = await client.get_user_profile_inferences("o_1")

    assert result == INFERENCES
    get_list.assert_awaited_once()
