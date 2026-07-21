"""services/cache.py 缓存服务测试

验证从 Phase 4 内联 aioredis.from_url 迁移到 RedisPool 后：
- 缓存命中/未命中
- bytes / str 返回值处理
- Redis 异常时降级返回 None（不崩溃）
- set_cached_briefing 正确写入 key + TTL
- 复盘缓存 round-trip 完整 ReviewArtifact 工件
- 旧纯文本缓存视为未命中
"""

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.market_trace import (
    MarketTraceResult,
    MarketTraceSnapshot,
    ReviewArtifact,
)
from aistock_agent.services import cache
from aistock_agent.services.cache import get_cached_review, set_cached_review

# ============================================================================
# 测试 fixture — REVIEW_ARTIFACT（最小合法 ReviewArtifact 实例）
# ============================================================================


class _SerializableReviewArtifact(ReviewArtifact):
    """model_dump() 默认返回 JSON 可序列化值，便于测试中 json.dumps。

    brief 测试使用 ``json.dumps(REVIEW_ARTIFACT.model_dump())`` 模拟 Redis
    返回的 JSON 字符串。``model_dump()`` 默认返回 ``datetime`` 等 Python 对象
    无法被 ``json.dumps`` 直接序列化，这里覆写为默认使用 ``mode="json"``。
    """

    def model_dump(self, **kwargs):  # type: ignore[override]
        kwargs.setdefault("mode", "json")
        return super().model_dump(**kwargs)


REVIEW_ARTIFACT = _SerializableReviewArtifact(
    schema_version="1.0",
    snapshot=MarketTraceSnapshot(
        snapshot_id="trace-20260719",
        trade_date="2026-07-19",
        captured_at=datetime(2026, 7, 19, 15, 0, tzinfo=UTC),
        a_share={},
        sources={},
        missing_fields=[],
        dominant_phenomenon=None,
    ),
    trace=MarketTraceResult(
        schema_version="1.0",
        dominant_phenomenon=None,
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="low",
        unresolved_questions=[],
    ),
    markdown="# A股收盘溯源\n快照编号：trace-20260719",
    trace_summary="测试摘要",
    sectors=["半导体", "贵金属"],
)


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

    artifact = {"snapshot": {"snapshot_id": "x"}, "markdown": "review content"}
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        await cache.set_cached_review("2026-07-19", artifact)

    mock_client.setex.assert_awaited_once_with(
        "briefing:review:2026-07-19", 86400, json.dumps(artifact, ensure_ascii=False),
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


# ============================================================================
# brief Step 1 — 复盘缓存 round-trip 完整工件 & 旧纯文本视为未命中
# ============================================================================


@pytest.mark.asyncio
async def test_review_cache_round_trips_full_artifact():
    client = AsyncMock()
    with patch("aistock_agent.services.cache.RedisPool") as pool:
        pool.get_client = AsyncMock(return_value=client)
        await set_cached_review("2026-07-19", REVIEW_ARTIFACT.model_dump())
        client.get = AsyncMock(return_value=json.dumps(REVIEW_ARTIFACT.model_dump()))
        restored = await get_cached_review("2026-07-19")
    assert restored["snapshot"]["snapshot_id"] == REVIEW_ARTIFACT.snapshot.snapshot_id


@pytest.mark.asyncio
async def test_legacy_markdown_review_cache_is_a_miss():
    client = AsyncMock()
    client.get = AsyncMock(return_value="# old markdown")
    with patch("aistock_agent.services.cache.RedisPool") as pool:
        pool.get_client = AsyncMock(return_value=client)
        assert await get_cached_review("2026-07-19") is None


# ============================================================================
# Task 5 review 修复 — set_cached_review 必须返回 bool，让 review 流程感知缓存成败
# ============================================================================


@pytest.mark.asyncio
async def test_set_cached_review_returns_true_on_success():
    """Redis 写入成功 → 返回 True。"""
    mock_client = AsyncMock()
    mock_client.setex = AsyncMock()
    artifact = {"snapshot": {"snapshot_id": "x"}, "markdown": "review content"}
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(return_value=mock_client)
        ok = await cache.set_cached_review("2026-07-19", artifact)
    assert ok is True


@pytest.mark.asyncio
async def test_set_cached_review_returns_false_on_redis_failure():
    """Redis 异常 → 返回 False（不向上抛，但调用方据此返回降级文本）。"""
    artifact = {"snapshot": {"snapshot_id": "x"}, "markdown": "review content"}
    with patch("aistock_agent.services.cache.RedisPool") as mock_pool:
        mock_pool.get_client = AsyncMock(side_effect=RuntimeError("pool not init"))
        ok = await cache.set_cached_review("2026-07-19", artifact)
    assert ok is False
