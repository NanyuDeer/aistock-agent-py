"""EventBus 单元测试 —— 验证 publish/consume/ack/retry/deadletter/idempotency。"""

from unittest.mock import AsyncMock

import pytest
from redis import exceptions as redis_exceptions

from aistock_agent.config import settings
from aistock_agent.services.event_bus import Event, EventBus, get_default_bus, set_default_bus


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
    return EventBus(
        mock_redis, max_retries=3, deadletter_prefix="dlq:", consumer_group="evening_chain"
    )


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
        (
            b"review_quick",
            [
                (
                    b"evt-1",
                    {b"payload": b'{"report_date":"2026-07-30"}', b"event_id": b"evt-1"},
                )
            ],
        )
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


# ============================================================================
# PEL 恢复（XAUTOCLAIM，不可用时降级 XPENDING + XCLAIM）
# ============================================================================


def _entry(msg_id: bytes, payload: str) -> tuple[bytes, dict[bytes, bytes]]:
    """构造 redis-py 归一后的 Stream 条目（与 xreadgroup/xautoclaim 同形）。"""
    return (msg_id, {b"payload": payload.encode("utf-8")})


@pytest.mark.asyncio
async def test_reclaim_pending_claims_idle_message_via_xautoclaim(event_bus, mock_redis):
    """空闲超阈值的 pending 消息被 XAUTOCLAIM 认领并归一为 Event。"""
    mock_redis.xautoclaim.return_value = [
        b"0-0",
        [_entry(b"evt-pel-1", '{"report_date":"2026-07-30"}')],
        [],
    ]

    events = await event_bus.reclaim_pending(
        "review_quick", "review_quick_consumer", min_idle_ms=300000, count=10
    )

    assert [e.event_id for e in events] == ["evt-pel-1"]
    assert events[0].channel == "review_quick"
    assert events[0].group == "evening_chain"
    assert events[0].payload["report_date"] == "2026-07-30"
    args = mock_redis.xautoclaim.call_args
    assert args.args[0] == "review_quick"
    assert args.args[1] == "evening_chain"
    assert args.args[2] == "review_quick_consumer"
    # min_idle_time 是唯一「不抢在途消息」的防线（服务端按 idle 过滤）→ 必须透传
    assert args.args[3] == 300000
    assert args.kwargs["count"] == 10
    # 走通 XAUTOCLAIM 时不触发降级路径
    mock_redis.xpending_range.assert_not_called()
    mock_redis.xclaim.assert_not_called()
    # 认领本身不 XACK：确认仍由处理成功后的既有分支负责
    mock_redis.xack.assert_not_called()


@pytest.mark.asyncio
async def test_reclaim_pending_skips_message_below_idle_threshold(event_bus, mock_redis):
    """未超阈值的在途消息不认领：Redis 按 min-idle 过滤后返回空 → 无事件、无 XACK。"""
    mock_redis.xautoclaim.return_value = [b"0-0", [], []]

    events = await event_bus.reclaim_pending("snapshot", "snapshot_consumer")

    assert events == []
    # 默认阈值来自配置，不能退化为 0（否则会抢回正在处理的消息）
    assert mock_redis.xautoclaim.call_args.args[3] == settings.event_bus_pel_min_idle_ms
    mock_redis.xack.assert_not_called()


@pytest.mark.asyncio
async def test_reclaim_pending_falls_back_to_xpending_xclaim(event_bus, mock_redis):
    """XAUTOCLAIM 不可用（Redis < 6.2）→ 降级 XPENDING + XCLAIM，仍能认领。"""
    mock_redis.xautoclaim.side_effect = redis_exceptions.ResponseError(
        "ERR unknown command 'XAUTOCLAIM'"
    )
    mock_redis.xpending_range.return_value = [[b"evt-pel-2", b"consumer-1", 600000, 1]]
    mock_redis.xclaim.return_value = [_entry(b"evt-pel-2", '{"report_date":"2026-07-30"}')]

    events = await event_bus.reclaim_pending(
        "review_done",
        "pred_consumer",
        group="prediction_chain",
        min_idle_ms=300000,
        count=5,
    )

    assert [e.event_id for e in events] == ["evt-pel-2"]
    assert events[0].group == "prediction_chain"
    assert mock_redis.xpending_range.call_args.args == (
        "review_done",
        "prediction_chain",
        "-",
        "+",
        5,
    )
    # 降级路径同样按 idle 过滤，避免抢在途消息
    assert mock_redis.xpending_range.call_args.kwargs == {"idle": 300000}
    assert mock_redis.xclaim.call_args.args[:4] == (
        "review_done",
        "prediction_chain",
        "pred_consumer",
        300000,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "xautoclaim_error",
    [
        redis_exceptions.ResponseError("ERR unknown command 'XAUTOCLAIM'"),
        AttributeError("'Redis' object has no attribute 'xautoclaim'"),
    ],
)
async def test_reclaim_pending_returns_empty_when_reclaim_unavailable(
    event_bus, mock_redis, xautoclaim_error
):
    """降级路径也失败时只告警、返回空：总线不可用不得影响主链路。"""
    mock_redis.xautoclaim.side_effect = xautoclaim_error
    mock_redis.xpending_range.side_effect = redis_exceptions.ConnectionError("redis down")

    assert await event_bus.reclaim_pending("broadcast", "broadcast_consumer") == []

    mock_redis.xclaim.assert_not_called()
    mock_redis.xack.assert_not_called()


@pytest.mark.asyncio
async def test_reclaim_pending_acks_unparseable_payload(event_bus, mock_redis):
    """毒消息（payload 非法 JSON）复用既有丢弃语义（XACK），避免被反复认领。"""
    mock_redis.xautoclaim.return_value = [
        b"0-0",
        [(b"evt-bad-1", {b"payload": b"not-json"})],
        [],
    ]

    assert await event_bus.reclaim_pending("review_quick", "review_quick_consumer") == []

    mock_redis.xack.assert_called_once_with("review_quick", "evening_chain", "evt-bad-1")
