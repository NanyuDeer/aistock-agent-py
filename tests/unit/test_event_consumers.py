"""Consumer 单元测试 — 验证 6 个 consumer 的事件处理逻辑。"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.agents.workers.review import ReviewRunResult
from aistock_agent.services.event_bus import Event, EventBus
from aistock_agent.services.event_consumers import (
    CHANNEL_BROADCAST,
    CHANNEL_ITERATE,
    CHANNEL_REVIEW_DONE,
    CHANNEL_REVIEW_FULL,
    CHANNEL_REVIEW_QUICK,
    CHANNEL_SNAPSHOT,
    BroadcastConsumer,
    ConsumerContext,
    IterateConsumer,
    PredictionConsumer,
    PredictionRetryExhaustedError,
    ReviewFullConsumer,
    ReviewQuickConsumer,
    SnapshotConsumer,
    start_all_consumers,
)
from aistock_agent.services.prediction_service import (
    PredictionRunResult,
    TraceUnavailableError,
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
    event = Event(
        event_id="evt-1",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="ok",
            report_date="2026-07-30",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="# Quick Review",
        )
        await consumer.handle(event)
        mock_run.assert_called_once()
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["snapshot_kind"] == "quick"
        assert call_kwargs["report_date"] == "2026-07-30"

    # status=ok → quick 改进版同样发布 review_done（次日预测，编排缺口 #1）与 snapshot
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    assert channels.count(CHANNEL_SNAPSHOT) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["snapshot_kind"] == "quick"


@pytest.mark.asyncio
async def test_review_full_consumer_calls_run_review_with_full(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewFullConsumer(ctx)
    event = Event(
        event_id="evt-2",
        channel=CHANNEL_REVIEW_FULL,
        payload={"report_date": "2026-07-30", "trace_id": "t2"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="ok",
            report_date="2026-07-30",
            snapshot_kind="full",
            trace_id="t2",
            markdown="# Full Review",
        )
        await consumer.handle(event)
        call_kwargs = mock_run.call_args[1]
        assert call_kwargs["snapshot_kind"] == "full"

    # status=ok → 同时发布 review_done 与 snapshot(full)
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    assert channels.count(CHANNEL_SNAPSHOT) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["snapshot_kind"] == "full"


@pytest.mark.asyncio
async def test_snapshot_consumer_publishes_iterate_for_full_kind(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = SnapshotConsumer(ctx)
    event = Event(
        event_id="evt-3",
        channel="snapshot",
        payload={"report_date": "2026-07-30", "snapshot_kind": "full"},
        group="evening_chain",
    )

    snapshot = {
        "date": "2026-07-30",
        "dimension_1_coverage": {"hit_rate": 0.85, "new_coverage_rate": 0.32},
        "data": {},
    }
    with patch("aistock_agent.services.event_consumers.build_snapshot", return_value=snapshot):
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
    event = Event(
        event_id="evt-4",
        channel="snapshot",
        payload={"report_date": "2026-07-30", "snapshot_kind": "quick"},
        group="evening_chain",
    )

    snapshot = {
        "date": "2026-07-30",
        "dimension_1_coverage": {"hit_rate": 0.85, "new_coverage_rate": 0.32},
        "data": {},
    }
    with patch("aistock_agent.services.event_consumers.build_snapshot", return_value=snapshot):
        await consumer.handle(event)

    # quick snapshot 不触发 iterate，但触发 broadcast（15:30 quick 晚间双人播报，2026-08-16 修复）
    mock_event_bus.publish.assert_called_once()
    publish_args = mock_event_bus.publish.call_args
    assert publish_args[0][0] == CHANNEL_BROADCAST
    # 绝不触发 iterate（iterate 是 full 复盘流水线）
    assert CHANNEL_ITERATE not in [c.args[0] for c in mock_event_bus.publish.await_args_list]
    # quick snapshot 同样持久化 brief_summary（晚间 brief 依赖）
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "market_snapshot"
    assert kwargs["content"]["brief_summary"] is not None


@pytest.mark.asyncio
async def test_iterate_consumer_publishes_broadcast(mock_event_bus, mock_node_api):
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = IterateConsumer(ctx)
    event = Event(
        event_id="evt-5",
        channel="iterate",
        payload={"report_date": "2026-07-30"},
        group="evening_chain",
    )

    with patch("aistock_agent.agents.workers.iterate.run", new_callable=AsyncMock) as mock_iter:
        mock_iter.return_value = {"final_response": '{"status":"normal","triggered_dimensions":[]}'}
        await consumer.handle(event)

    mock_event_bus.publish.assert_called_once_with(
        "broadcast", payload={"report_date": "2026-07-30"}
    )
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
    event = Event(
        event_id="evt-6",
        channel="broadcast",
        payload={"report_date": "2026-07-30"},
        group="evening_chain",
    )

    with (
        patch("aistock_agent.agents.workers.broadcast.run", new_callable=AsyncMock) as mock_bc,
        patch(
            "aistock_agent.services.event_consumers.build_and_persist_brief",
            new_callable=AsyncMock,
        ) as mock_brief,
    ):
        mock_brief.return_value = True
        await consumer.handle(event)
        mock_bc.assert_called_once()


# ============================================================================
# review_done 发布规则（PR-A/T2）
# ============================================================================


@pytest.mark.asyncio
async def test_review_full_consumer_publishes_review_done_on_ok(mock_event_bus, mock_node_api):
    """run_review status=ok → 发布 review_done（幂等 event_id=review_done_{date}_{trace_id}）。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewFullConsumer(ctx)
    event = Event(
        event_id="evt-full-ok",
        channel=CHANNEL_REVIEW_FULL,
        payload={"report_date": "2026-07-30", "trace_id": "t2"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="ok",
            report_date="2026-07-30",
            snapshot_kind="full",
            trace_id="t2",
            markdown="# Full",
        )
        await consumer.handle(event)

    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    review_done_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_REVIEW_DONE
    )
    assert review_done_call.kwargs["event_id"] == "review_done_2026-07-30_t2"
    assert review_done_call.kwargs["payload"] == {"report_date": "2026-07-30", "trace_id": "t2"}
    # 既有 snapshot 链路不受影响
    assert channels.count(CHANNEL_SNAPSHOT) == 1


@pytest.mark.asyncio
async def test_review_full_consumer_skips_review_done_on_degraded(mock_event_bus, mock_node_api):
    """run_review status=degraded → 不发布 review_done（硬约束 6），snapshot 链路照常。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewFullConsumer(ctx)
    event = Event(
        event_id="evt-full-degraded",
        channel=CHANNEL_REVIEW_FULL,
        payload={"report_date": "2026-07-30", "trace_id": "t2"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="degraded",
            report_date="2026-07-30",
            snapshot_kind="full",
            trace_id="t2",
            markdown="",
        )
        await consumer.handle(event)

    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert CHANNEL_REVIEW_DONE not in channels
    assert channels.count(CHANNEL_SNAPSHOT) == 1


@pytest.mark.asyncio
async def test_review_full_consumer_skips_review_done_on_skipped(mock_event_bus, mock_node_api):
    """run_review status=skipped → 不发布 review_done（硬约束 6），snapshot 链路照常。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewFullConsumer(ctx)
    event = Event(
        event_id="evt-full-skipped",
        channel=CHANNEL_REVIEW_FULL,
        payload={"report_date": "2026-07-30", "trace_id": "t2"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="skipped",
            report_date="2026-07-30",
            snapshot_kind="full",
            trace_id="t2",
            markdown="",
        )
        await consumer.handle(event)

    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert CHANNEL_REVIEW_DONE not in channels
    assert channels.count(CHANNEL_SNAPSHOT) == 1


@pytest.mark.asyncio
async def test_review_quick_consumer_degraded_exhausts_publishes_degraded_snapshot(
    mock_event_bus, mock_node_api
):
    """恒 degraded → 退避重试 3 次耗尽 → 仍发 snapshot(review_degraded=true)、不发 review_done。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-degraded",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with (
        patch(
            "aistock_agent.services.event_consumers.run_review",
            new_callable=AsyncMock,
        ) as mock_run,
        patch(
            "aistock_agent.services.event_consumers.REVIEW_QUICK_RETRY_BACKOFF", (0, 0)
        ),
    ):
        mock_run.return_value = ReviewRunResult(
            status="degraded",
            report_date="2026-07-30",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="",
        )
        await consumer.handle(event)

    assert mock_run.await_count == 3
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert CHANNEL_REVIEW_DONE not in channels
    assert channels.count(CHANNEL_SNAPSHOT) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["review_degraded"] is True
    assert snapshot_call.kwargs["payload"]["review_status"] == "degraded"


# ============================================================================
# PredictionConsumer（独立消费组 prediction_chain，PR-A/T2）
# ============================================================================


def _prediction_event(report_date: str = "2026-07-30", trace_id: str = "t1") -> Event:
    return Event(
        event_id=f"review_done_{report_date}_{trace_id}",
        channel=CHANNEL_REVIEW_DONE,
        payload={"report_date": report_date, "trace_id": trace_id},
        group="prediction_chain",
    )


@pytest.mark.asyncio
async def test_prediction_consumer_ok_no_retry(mock_event_bus, mock_node_api):
    """status=ok → predict_from_trace 恰好一次、不抛（ok 已由 predict_from_trace 内落库）。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = PredictionConsumer(ctx)

    with patch(
        "aistock_agent.services.event_consumers.predict_from_trace",
        new_callable=AsyncMock,
    ) as mock_predict:
        mock_predict.return_value = (
            PredictionRunResult(
                status="ok", prediction=MagicMock(), due_dates={"short": "2026-08-06"}
            ),
            {"id": "pred-1"},
        )
        await consumer.handle(_prediction_event())

    mock_predict.assert_awaited_once_with("t1", "2026-07-30")


@pytest.mark.asyncio
async def test_prediction_consumer_llm_failed_then_ok_retries_once(mock_event_bus, mock_node_api):
    """llm_failed → retry-once（退避后二次调用）→ ok → 不抛。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = PredictionConsumer(ctx)

    with (
        patch(
            "aistock_agent.services.event_consumers.predict_from_trace",
            new_callable=AsyncMock,
        ) as mock_predict,
        patch("aistock_agent.services.event_consumers.PREDICTION_RETRY_BACKOFF_SEC", 0),
    ):
        mock_predict.side_effect = [
            (PredictionRunResult(status="llm_failed", reason="boom"), None),
            (
                PredictionRunResult(status="ok", prediction=MagicMock(), due_dates={}),
                {"id": "pred-1"},
            ),
        ]
        await consumer.handle(_prediction_event())  # 不抛

    assert mock_predict.await_count == 2


@pytest.mark.asyncio
async def test_prediction_consumer_llm_failed_exhausts_raises(mock_event_bus, mock_node_api):
    """llm_failed → retry-once 后仍失败 → 抛 PredictionRetryExhaustedError。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = PredictionConsumer(ctx)

    with (
        patch(
            "aistock_agent.services.event_consumers.predict_from_trace",
            new_callable=AsyncMock,
        ) as mock_predict,
        patch("aistock_agent.services.event_consumers.PREDICTION_RETRY_BACKOFF_SEC", 0),
    ):
        mock_predict.return_value = (PredictionRunResult(status="llm_failed", reason="boom"), None)
        with pytest.raises(PredictionRetryExhaustedError):
            await consumer.handle(_prediction_event())

    assert mock_predict.await_count == 2


@pytest.mark.asyncio
async def test_prediction_consumer_gate_skipped_no_retry(mock_event_bus, mock_node_api):
    """gate_skipped → 一次调用、不重试、不抛（skipped 已由 predict_from_trace 内落库）。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = PredictionConsumer(ctx)

    with patch(
        "aistock_agent.services.event_consumers.predict_from_trace",
        new_callable=AsyncMock,
    ) as mock_predict:
        mock_predict.return_value = (
            PredictionRunResult(status="gate_skipped", reason="attribution_status=not_applicable"),
            None,
        )
        await consumer.handle(_prediction_event())

    mock_predict.assert_awaited_once_with("t1", "2026-07-30")


@pytest.mark.asyncio
async def test_prediction_consumer_trace_unavailable_skips(mock_event_bus, mock_node_api):
    """TraceUnavailableError → save_skipped_prediction 被调、不抛、不重试（硬约束 7）。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = PredictionConsumer(ctx)

    with (
        patch(
            "aistock_agent.services.event_consumers.predict_from_trace",
            new_callable=AsyncMock,
        ) as mock_predict,
        patch(
            "aistock_agent.services.event_consumers.save_skipped_prediction",
            new_callable=AsyncMock,
        ) as mock_skip,
    ):
        mock_predict.side_effect = TraceUnavailableError("no trace available for review:2026-07-30")
        await consumer.handle(_prediction_event())

    mock_skip.assert_awaited_once_with(
        "review:2026-07-30", "no trace available for review:2026-07-30"
    )
    mock_predict.assert_awaited_once_with("t1", "2026-07-30")


@pytest.mark.asyncio
async def test_start_all_consumers_registers_six_with_prediction_group(
    mock_event_bus, mock_node_api
):
    """start_all_consumers 注册 6 个消费者；PredictionConsumer 用独立组 prediction_chain。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)

    with patch(
        "aistock_agent.services.event_consumers._consumer_loop",
        new_callable=AsyncMock,
    ) as mock_loop:
        tasks = start_all_consumers(ctx)
        assert len(tasks) == 6
        await asyncio.gather(*tasks, return_exceptions=True)

    assert len(mock_loop.await_args_list) == 6
    pred_calls = [c for c in mock_loop.await_args_list if c.args[0].channel == CHANNEL_REVIEW_DONE]
    assert len(pred_calls) == 1
    assert pred_calls[0].kwargs["group"] == "prediction_chain"
    non_pred_calls = [
        c for c in mock_loop.await_args_list if c.args[0].channel != CHANNEL_REVIEW_DONE
    ]
    assert len(non_pred_calls) == 5
    assert all(c.kwargs.get("group") is None for c in non_pred_calls)


@pytest.mark.asyncio
async def test_review_quick_consumer_retries_then_ok(mock_event_bus, mock_node_api):
    """degraded → 按退避重试 → ok → 发 snapshot(review_degraded=false) + review_done。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-retry-ok",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with (
        patch(
            "aistock_agent.services.event_consumers.run_review",
            new_callable=AsyncMock,
        ) as mock_run,
        patch(
            "aistock_agent.services.event_consumers.REVIEW_QUICK_RETRY_BACKOFF", (0, 0)
        ),
    ):
        mock_run.side_effect = [
            ReviewRunResult(
                status="degraded",
                report_date="2026-07-30",
                snapshot_kind="quick",
                trace_id="t1",
                markdown="",
            ),
            ReviewRunResult(
                status="ok",
                report_date="2026-07-30",
                snapshot_kind="quick",
                trace_id="t1",
                markdown="# Quick",
            ),
        ]
        await consumer.handle(event)

    assert mock_run.await_count == 2
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    assert channels.count(CHANNEL_SNAPSHOT) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["review_degraded"] is False


@pytest.mark.asyncio
async def test_review_quick_consumer_skipped_no_retry(mock_event_bus, mock_node_api):
    """skipped(已有 full) → 不重试、发 snapshot、不发 review_done。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-skipped",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="skipped",
            report_date="2026-07-30",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="",
        )
        await consumer.handle(event)

    assert mock_run.await_count == 1
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert CHANNEL_REVIEW_DONE not in channels
    assert channels.count(CHANNEL_SNAPSHOT) == 1


@pytest.mark.asyncio
async def test_review_quick_consumer_ok_no_retry_publishes_review_done(
    mock_event_bus, mock_node_api
):
    """首次即 ok → 只调 1 次、发 review_done + snapshot(review_degraded=false)。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-quick-ok",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "2026-07-30", "trace_id": "t1"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="ok",
            report_date="2026-07-30",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="# Quick",
        )
        await consumer.handle(event)

    assert mock_run.await_count == 1
    channels = [c.args[0] for c in mock_event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    snapshot_call = next(
        c for c in mock_event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["review_degraded"] is False


@pytest.mark.asyncio
async def test_snapshot_consumer_degraded_fallback_on_missing_reports(
    mock_event_bus, mock_node_api
):
    """build_snapshot 返回 error → 不 raise、持久化降级快照、quick 仍发布 broadcast。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = SnapshotConsumer(ctx)
    event = Event(
        event_id="evt-snap-degraded",
        channel="snapshot",
        payload={"report_date": "2026-07-30", "snapshot_kind": "quick"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.build_snapshot",
        return_value={"error": "missing_reports"},
    ):
        await consumer.handle(event)  # 不得抛异常

    mock_event_bus.publish.assert_called_once()
    assert mock_event_bus.publish.call_args[0][0] == CHANNEL_BROADCAST
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "market_snapshot"
    # 降级快照仍能生成可播报的 brief_summary，且带降级标记
    assert kwargs["content"]["brief_summary"] is not None
    assert kwargs["content"]["snapshot"]["degraded"] is True


@pytest.mark.asyncio
async def test_snapshot_consumer_degraded_full_publishes_iterate(mock_event_bus, mock_node_api):
    """build_snapshot 返回 error 且 snapshot_kind=full → 不 raise、发 iterate（不 broadcast）。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = SnapshotConsumer(ctx)
    event = Event(
        event_id="evt-snap-degraded-full",
        channel="snapshot",
        payload={"report_date": "2026-07-30", "snapshot_kind": "full"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.build_snapshot",
        return_value={"error": "missing_reports"},
    ):
        await consumer.handle(event)  # 不得抛异常

    # full 降级快照仍走原 snapshot_kind 分派 → iterate 链路（而非 broadcast）
    mock_event_bus.publish.assert_called_once_with(
        CHANNEL_ITERATE, payload={"report_date": "2026-07-30"}
    )
    assert CHANNEL_BROADCAST not in [c.args[0] for c in mock_event_bus.publish.await_args_list]
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "market_snapshot"
    assert kwargs["content"]["snapshot"]["degraded"] is True


@pytest.mark.asyncio
async def test_snapshot_consumer_review_degraded_forces_degraded(mock_event_bus, mock_node_api):
    """review_degraded=true 且 build_snapshot 正常（无 error）→ 仍强制降级并透传降级字段。"""
    ctx = ConsumerContext(mock_event_bus, mock_node_api)
    consumer = SnapshotConsumer(ctx)
    event = Event(
        event_id="evt-snap-review-degraded",
        channel="snapshot",
        payload={
            "report_date": "2026-07-30",
            "snapshot_kind": "quick",
            "review_degraded": True,
            "review_status": "degraded",
        },
        group="evening_chain",
    )

    snapshot = {
        "date": "2026-07-30",
        "dimension_1_coverage": {"hit_rate": 0.85, "new_coverage_rate": 0.32},
        "data": {},
    }
    with patch(
        "aistock_agent.services.event_consumers.build_snapshot",
        return_value=snapshot,
    ):
        await consumer.handle(event)

    # quick → broadcast，分派不受 review_degraded 影响
    mock_event_bus.publish.assert_called_once_with(
        CHANNEL_BROADCAST, payload={"report_date": "2026-07-30"}
    )
    _, kwargs = mock_node_api.save_analysis_report.call_args
    assert kwargs["report_type"] == "market_snapshot"
    # 降级契约显式写入持久化内容（而非透传不透用）
    assert kwargs["content"]["review_degraded"] is True
    assert kwargs["content"]["review_status"] == "degraded"
    # build_snapshot 虽成功，但 review 已降级 → 快照仍被标记为降级
    assert kwargs["content"]["snapshot"]["degraded"] is True
