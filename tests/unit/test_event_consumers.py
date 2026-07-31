"""Consumer 单元测试 — 验证 5 个 consumer 的事件处理逻辑。"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aistock_agent.services.event_bus import EventBus, Event
from aistock_agent.services.event_consumers import (
    ConsumerContext,
    ReviewQuickConsumer,
    ReviewFullConsumer,
    SnapshotConsumer,
    IterateConsumer,
    BroadcastConsumer,
)


@pytest.fixture
def mock_event_bus():
    bus = AsyncMock(spec=EventBus)
    bus.publish = AsyncMock(return_value="evt-new")
    bus.ack = AsyncMock()
    bus.retry = AsyncMock()
    return bus


@pytest.fixture
def mock_node_api():
    api = AsyncMock()
    api.get_analysis_report = AsyncMock(return_value=None)
    api.save_analysis_report = AsyncMock(return_value={"code": 200})
    return api


@pytest.mark.asyncio
async def test_review_quick_consumer_calls_run_review_with_quick(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(event_id="evt-1", channel="review_quick", payload={"report_date": "2026-07-30", "trace_id": "t1"})

    with patch("aistock_agent.services.event_consumers.run_review", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"status": "ok", "markdown": "# Quick Review"}
        await consumer.handle(event)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["snapshot_kind"] == "quick"
        assert call_kwargs["report_date"] == "2026-07-30"

    # 完成后应该 publish snapshot 事件
    mock_event_bus.publish.assert_called_once()
    pub_args = mock_event_bus.publish.call_args
    assert pub_args[0][0] == "snapshot"
    assert pub_args[1]["payload"]["snapshot_kind"] == "quick"


@pytest.mark.asyncio
async def test_review_full_consumer_calls_run_review_with_full(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewFullConsumer(ctx)
    event = Event(event_id="evt-2", channel="review_full", payload={"report_date": "2026-07-30", "trace_id": "t2"})

    with patch("aistock_agent.services.event_consumers.run_review", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"status": "ok", "markdown": "# Full Review"}
        await consumer.handle(event)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["snapshot_kind"] == "full"

    mock_event_bus.publish.assert_called_once()
    assert mock_event_bus.publish.call_args[1]["payload"]["snapshot_kind"] == "full"


@pytest.mark.asyncio
async def test_snapshot_consumer_publishes_iterate_for_full_kind(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = SnapshotConsumer(ctx)
    event = Event(event_id="evt-3", channel="snapshot", payload={"report_date": "2026-07-30", "snapshot_kind": "full"})

    snapshot = {
        "date": "2026-07-30",
        "dimension_1_coverage": {"hit_rate": 0.85, "new_coverage_rate": 0.32},
        "data": {},
    }
    with patch("aistock_agent.services.event_consumers.build_snapshot", return_value=snapshot) as mock_snap:
        await consumer.handle(event)

    # full snapshot 完成后触发 iterate
    mock_event_bus.publish.assert_called_once_with("iterate", payload={"report_date": "2026-07-30"})
    # 持久化 content 必须携带受控 brief_summary，否则 brief_evening 会降级
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "market_snapshot"
    brief_summary = kwargs["content"]["brief_summary"]
    assert brief_summary is not None
    assert brief_summary["schema_version"] == "brief_summary.v1"
    assert brief_summary["report_type"] == "market_snapshot"
    assert "市场快照" in brief_summary["summary"]


@pytest.mark.asyncio
async def test_snapshot_consumer_skips_iterate_for_quick_kind(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = SnapshotConsumer(ctx)
    event = Event(event_id="evt-4", channel="snapshot", payload={"report_date": "2026-07-30", "snapshot_kind": "quick"})

    snapshot = {
        "date": "2026-07-30",
        "dimension_1_coverage": {"hit_rate": 0.85, "new_coverage_rate": 0.32},
        "data": {},
    }
    with patch("aistock_agent.services.event_consumers.build_snapshot", return_value=snapshot) as mock_snap:
        await consumer.handle(event)

    # quick snapshot 不触发 iterate
    mock_event_bus.publish.assert_not_called()
    # quick snapshot 同样持久化 brief_summary（晚间 brief 依赖）
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "market_snapshot"
    assert kwargs["content"]["brief_summary"] is not None


@pytest.mark.asyncio
async def test_iterate_consumer_publishes_broadcast(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = IterateConsumer(ctx)
    event = Event(event_id="evt-5", channel="iterate", payload={"report_date": "2026-07-30"})

    with patch("aistock_agent.agents.workers.iterate.run", new_callable=AsyncMock) as mock_iter:
        mock_iter.return_value = {"final_response": '{"status":"normal","triggered_dimensions":[]}'}
        await consumer.handle(event)

    mock_event_bus.publish.assert_called_once_with("broadcast", payload={"report_date": "2026-07-30"})
    # 持久化 content 必须携带受控 brief_summary，否则 brief_evening 会降级
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "iterate"
    brief_summary = kwargs["content"]["brief_summary"]
    assert brief_summary is not None
    assert brief_summary["schema_version"] == "brief_summary.v1"
    assert brief_summary["report_type"] == "iterate"
    assert brief_summary["summary"] == "今日无显著异常"


@pytest.mark.asyncio
async def test_broadcast_consumer_calls_broadcast_run(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = BroadcastConsumer(ctx)
    event = Event(event_id="evt-6", channel="broadcast", payload={"report_date": "2026-07-30"})

    with (
        patch("aistock_agent.agents.workers.broadcast.run", new_callable=AsyncMock) as mock_bc,
        patch("aistock_agent.services.event_consumers.build_and_persist_brief", new_callable=AsyncMock) as mock_brief,
    ):
        mock_brief.return_value = True
        await consumer.handle(event)
        mock_bc.assert_called_once()
