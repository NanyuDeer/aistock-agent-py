"""EventBus 单元测试 —— 验证 publish/consume/ack/retry/deadletter/idempotency。"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from aistock_agent.services.event_bus import EventBus, Event, get_default_bus, set_default_bus


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
    event = Event(
        event_id="evt-1",
        channel="review_quick",
        payload={"retry_count": 0},
        group="evening_chain",
    )
    await event_bus.retry(event)
    mock_redis.xadd.assert_called()
    call_args = mock_redis.xadd.call_args
    payload = call_args[0][1]
    assert payload["retry_count"] == 1


@pytest.mark.asyncio
async def test_retry_exceeds_max_goes_to_deadletter(event_bus, mock_redis):
    event = Event(
        event_id="evt-1",
        channel="review_quick",
        payload={"retry_count": 3},
        group="evening_chain",
    )
    await event_bus.retry(event)
    # 应该写入 dlq:review_quick 而非原 channel
    assert mock_redis.xadd.call_args[0][0] == "dlq:review_quick"


@pytest.mark.asyncio
async def test_mark_deadletter_uses_event_group(event_bus, mock_redis):
    """mark_deadletter 内部 ack 使用 event.group（而非总线默认组 evening_chain）。"""
    event = Event(
        event_id="evt-1",
        channel="review_done",
        payload={"retry_count": 3},
        group="prediction_chain",
    )
    await event_bus.mark_deadletter(event, reason="max_retries_exceeded:4")
    # 应写入死信队列 dlq:review_done
    assert mock_redis.xadd.call_args.args[0] == "dlq:review_done"
    # ack 必须使用 event.group，避免消息在 prediction_chain 中永久 pending
    mock_redis.xack.assert_called_once_with("review_done", "prediction_chain", "evt-1")


@pytest.mark.asyncio
async def test_retry_to_deadletter_uses_event_group(event_bus, mock_redis):
    """retry 超限走 mark_deadletter 时，内部 ack 同样使用 event.group。"""
    event = Event(
        event_id="evt-1",
        channel="review_done",
        payload={"retry_count": 3},
        group="prediction_chain",
    )
    await event_bus.retry(event)
    # xadd 是死信队列写入
    assert mock_redis.xadd.call_args.args[0] == "dlq:review_done"
    mock_redis.xack.assert_called_once_with("review_done", "prediction_chain", "evt-1")


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


# ============================================================================
# 消费者组参数化（PR-A/T1）
# ============================================================================


@pytest.mark.asyncio
async def test_consume_uses_custom_group(event_bus, mock_redis):
    """consume 传自定义组时 xreadgroup 使用该组，且 Event.group 被正确填充。"""
    mock_redis.xreadgroup.return_value = [
        (
            b"review_done",
            [(b"evt-9", {b"payload": b'{"report_date":"2026-07-30"}', b"event_id": b"evt-9"})],
        )
    ]
    events = await event_bus.consume("review_done", "pred-consumer", group="prediction_chain")

    # _ensure_group 与 xreadgroup 都应使用自定义组
    mock_redis.xgroup_create.assert_called_once_with(
        "review_done", "prediction_chain", id="0", mkstream=True
    )
    assert mock_redis.xreadgroup.call_args.args[0] == "prediction_chain"
    assert len(events) == 1
    assert events[0].group == "prediction_chain"


@pytest.mark.asyncio
async def test_ack_explicit_group(event_bus, mock_redis):
    """ack 显式传组时 xack 使用该组。"""
    await event_bus.ack("review_done", "evt-1", group="prediction_chain")
    mock_redis.xack.assert_called_once_with("review_done", "prediction_chain", "evt-1")


@pytest.mark.asyncio
async def test_retry_uses_event_group(event_bus, mock_redis):
    """retry 内部 ack 使用 event.group（而非总线默认组）。"""
    event = Event(
        event_id="evt-1",
        channel="review_done",
        payload={"retry_count": 0},
        group="prediction_chain",
    )
    await event_bus.retry(event)
    mock_redis.xack.assert_called_once_with("review_done", "prediction_chain", "evt-1")


def test_default_bus_accessors(event_bus):
    """set_default_bus / get_default_bus 存取。"""
    set_default_bus(None)  # 干净起点
    assert get_default_bus() is None
    set_default_bus(event_bus)
    assert get_default_bus() is event_bus
    set_default_bus(None)  # 还原，避免影响其他测试
    assert get_default_bus() is None
