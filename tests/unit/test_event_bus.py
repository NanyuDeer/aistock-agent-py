"""EventBus 单元测试 —— 验证 publish/consume/ack/retry/deadletter/idempotency。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from aistock_agent.services.event_bus import EventBus, Event


@pytest.fixture
def mock_redis():
    """构造 mock Redis 客户端（模拟 XADD/XREADGROUP/XACK 等）。"""
    client = AsyncMock()
    client.xadd = AsyncMock(return_value=b"1234-0")
    client.xreadgroup = AsyncMock(return_value=[])
    client.xack = AsyncMock(return_value=1)
    client.set = AsyncMock(return_value=True)
    client.setex = AsyncMock()
    client.get = AsyncMock(return_value=None)
    return client


@pytest.fixture
def event_bus(mock_redis):
    return EventBus(mock_redis, max_retries=3, deadletter_prefix="dlq:", consumer_group="evening_chain")


@pytest.mark.asyncio
async def test_publish_returns_event_id(event_bus, mock_redis):
    mock_redis.xadd.return_value = b"evt-123-0"
    event_id = await event_bus.publish("review_quick", {"report_date": "2026-07-30"})
    assert event_id == "evt-123-0"
    mock_redis.xadd.assert_called_once()
    args = mock_redis.xadd.call_args
    assert args[0][0] == "review_quick"
    assert args[1]["maxlen"] == 10000


@pytest.mark.asyncio
async def test_publish_sets_idempotency_key(event_bus, mock_redis):
    await event_bus.publish("review_quick", {"report_date": "2026-07-30"}, event_id="evt-001")
    # 幂等 key 应该被设置（用 setex 带 TTL）
    mock_redis.setex.assert_called()
    call_args = mock_redis.setex.call_args
    assert "evt-001" in str(call_args)


@pytest.mark.asyncio
async def test_consume_returns_events(event_bus, mock_redis):
    mock_redis.xreadgroup.return_value = [
        (b"review_quick", [(b"evt-1", {b"payload": b'{"report_date":"2026-07-30"}', b"event_id": b"evt-1"})])
    ]
    events = await event_bus.consume("review_quick", "consumer-1")
    assert len(events) == 1
    assert events[0].event_id == "evt-1"
    assert events[0].payload["report_date"] == "2026-07-30"


@pytest.mark.asyncio
async def test_consume_returns_empty_when_no_events(event_bus, mock_redis):
    mock_redis.xreadgroup.return_value = []
    events = await event_bus.consume("review_quick", "consumer-1", block_ms=100)
    assert events == []


@pytest.mark.asyncio
async def test_ack_confirms_event(event_bus, mock_redis):
    await event_bus.ack("review_quick", "evt-1")
    mock_redis.xack.assert_called_once_with("review_quick", "evening_chain", "evt-1")


@pytest.mark.asyncio
async def test_retry_republishes_with_incremented_count(event_bus, mock_redis):
    event = Event(event_id="evt-1", channel="review_quick", payload={"retry_count": 0})
    await event_bus.retry(event)
    mock_redis.xadd.assert_called()
    call_args = mock_redis.xadd.call_args
    payload = call_args[0][1]
    assert payload["retry_count"] == 1


@pytest.mark.asyncio
async def test_retry_exceeds_max_goes_to_deadletter(event_bus, mock_redis):
    event = Event(event_id="evt-1", channel="review_quick", payload={"retry_count": 3})
    await event_bus.retry(event)
    # 应该写入 dlq:review_quick 而非原 channel
    assert mock_redis.xadd.call_args[0][0] == "dlq:review_quick"


@pytest.mark.asyncio
async def test_is_processed_returns_true_when_key_exists(event_bus, mock_redis):
    mock_redis.get.return_value = b"1"
    assert await event_bus.is_processed("evt-1") is True


@pytest.mark.asyncio
async def test_is_processed_returns_false_when_key_absent(event_bus, mock_redis):
    mock_redis.get.return_value = None
    assert await event_bus.is_processed("evt-1") is False


@pytest.mark.asyncio
async def test_mark_processed_sets_key_with_ttl(event_bus, mock_redis):
    await event_bus.mark_processed("evt-1", ttl_seconds=3600)
    mock_redis.setex.assert_called_once()
    call_args = mock_redis.setex.call_args
    assert call_args[0][1] == 3600
    assert "evt-1" in call_args[0][0]
