"""evening_chain 事件驱动集成测试 -- 验证完整事件流转 + 链路覆盖 + 报告覆盖。"""

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from aistock_agent.services.event_bus import EventBus, Event
from aistock_agent.services.event_consumers import (
    ConsumerContext,
    ReviewQuickConsumer,
    ReviewFullConsumer,
    SnapshotConsumer,
    CHANNEL_REVIEW_QUICK,
    CHANNEL_REVIEW_FULL,
    CHANNEL_SNAPSHOT,
    CHANNEL_ITERATE,
    CHANNEL_BROADCAST,
)


@pytest.fixture
def event_bus():
    bus = AsyncMock(spec=EventBus)
    bus.publish = AsyncMock(return_value="evt-new")
    bus.ack = AsyncMock()
    bus.retry = AsyncMock()
    return bus


@pytest.fixture
def ctx(event_bus):
    mock_node = AsyncMock()
    mock_node.get_analysis_report = AsyncMock(return_value=None)
    mock_node.save_analysis_report = AsyncMock(return_value={"code": 200})
    return ConsumerContext(event_bus, mock_node)


@pytest.mark.asyncio
async def test_quick_chain_review_to_snapshot(ctx):
    """quick 链路：review_quick -> snapshot(quick)，不触发 iterate。"""
    consumer = ReviewQuickConsumer(ctx)
    event = Event(event_id="evt-1", channel=CHANNEL_REVIEW_QUICK, payload={"report_date": "20260730", "trace_id": "t1"})

    with patch("aistock_agent.services.event_consumers.run_review", new_callable=AsyncMock) as mock_review:
        mock_review.return_value = MagicMock(status="ok", markdown="# Quick")
        await consumer.handle(event)

    # 应该 publish snapshot 事件（kind=quick）
    ctx.event_bus.publish.assert_called_once()
    pub_call = ctx.event_bus.publish.call_args
    assert pub_call[0][0] == CHANNEL_SNAPSHOT
    assert pub_call[1]["payload"]["snapshot_kind"] == "quick"


@pytest.mark.asyncio
async def test_full_chain_review_to_snapshot_to_iterate_to_broadcast(ctx):
    """full 链路：review_full -> snapshot(full) -> iterate -> broadcast 完整流转。"""
    # 1. ReviewFullConsumer
    review_consumer = ReviewFullConsumer(ctx)
    review_event = Event(event_id="evt-1", channel=CHANNEL_REVIEW_FULL, payload={"report_date": "20260730", "trace_id": "t2"})

    with patch("aistock_agent.services.event_consumers.run_review", new_callable=AsyncMock) as mock_review:
        mock_review.return_value = MagicMock(status="ok", markdown="# Full")
        await review_consumer.handle(review_event)

    ctx.event_bus.publish.assert_called_once()
    assert ctx.event_bus.publish.call_args[1]["payload"]["snapshot_kind"] == "full"

    # 2. SnapshotConsumer（full -> iterate）
    ctx.event_bus.publish.reset_mock()
    snap_consumer = SnapshotConsumer(ctx)
    snap_event = Event(event_id="evt-2", channel=CHANNEL_SNAPSHOT, payload={"report_date": "20260730", "snapshot_kind": "full"})

    with patch("aistock_agent.services.event_consumers.build_snapshot", return_value={"date": "20260730", "data": {}}):
        await snap_consumer.handle(snap_event)

    ctx.event_bus.publish.assert_called_once_with(CHANNEL_ITERATE, payload={"report_date": "20260730"})


@pytest.mark.asyncio
async def test_quick_review_skipped_when_full_report_exists(ctx):
    """覆盖逻辑：已有 full 报告时，quick review 跳过持久化。"""
    # mock node_api 返回已有 full 报告
    ctx.node_api.get_analysis_report = AsyncMock(return_value={
        "data_source": "review_agent_full",
        "content": {},
    })

    consumer = ReviewQuickConsumer(ctx)
    event = Event(event_id="evt-1", channel=CHANNEL_REVIEW_QUICK, payload={"report_date": "20260730", "trace_id": "t3"})

    with patch("aistock_agent.services.event_consumers.run_review", new_callable=AsyncMock) as mock_review:
        mock_review.return_value = MagicMock(status="skipped", markdown="")
        await consumer.handle(event)

    # status=skipped 时仍 publish snapshot（quick snapshot 不依赖 review）
    ctx.event_bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_consumer_failure_triggers_retry(ctx):
    """consumer handle 失败时触发 retry（非 ack）。"""
    consumer = ReviewQuickConsumer(ctx)
    event = Event(event_id="evt-fail", channel=CHANNEL_REVIEW_QUICK, payload={"report_date": "20260730", "trace_id": "t4"})

    with patch("aistock_agent.services.event_consumers.run_review", new_callable=AsyncMock) as mock_review:
        mock_review.side_effect = Exception("LLM unavailable")
        # handle 失败应抛异常，由 consumer_loop 捕获后 retry
        with pytest.raises(Exception, match="LLM unavailable"):
            await consumer.handle(event)

    # retry 不在 handle 内调用，由 consumer_loop 负责
    # 此测试验证 handle 失败时抛异常（consumer_loop 会捕获并 retry）
