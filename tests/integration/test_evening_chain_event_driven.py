"""evening_chain 事件驱动集成测试 -- 验证完整事件流转 + 链路覆盖 + 报告覆盖。"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.review import ReviewRunResult
from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult
from aistock_agent.services.event_bus import Event, EventBus
from aistock_agent.services.event_consumers import (
    CHANNEL_ITERATE,
    CHANNEL_REVIEW_DONE,
    CHANNEL_REVIEW_FULL,
    CHANNEL_REVIEW_QUICK,
    CHANNEL_SNAPSHOT,
    ConsumerContext,
    PredictionConsumer,
    ReviewFullConsumer,
    ReviewQuickConsumer,
    SnapshotConsumer,
)
from aistock_agent.services.prediction_service import PredictionRunResult


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
    """quick 链路：review_quick -> snapshot(quick)，不触发 iterate，不发 review_done。"""
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-1",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "20260730", "trace_id": "t1"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_review:
        mock_review.return_value = ReviewRunResult(
            status="ok",
            report_date="20260730",
            snapshot_kind="quick",
            trace_id="t1",
            markdown="# Quick",
        )
        await consumer.handle(event)

    # 应该 publish snapshot 事件（kind=quick）
    ctx.event_bus.publish.assert_called_once()
    pub_call = ctx.event_bus.publish.call_args
    assert pub_call[0][0] == CHANNEL_SNAPSHOT
    assert pub_call[1]["payload"]["snapshot_kind"] == "quick"
    # quick 链路不发 review_done（S1）
    assert CHANNEL_REVIEW_DONE not in [c.args[0] for c in ctx.event_bus.publish.await_args_list]


@pytest.mark.asyncio
async def test_full_chain_review_to_snapshot_to_iterate_to_broadcast(ctx):
    """full 链路：review_full -> snapshot(full) -> iterate -> broadcast 完整流转。"""
    # 1. ReviewFullConsumer
    review_consumer = ReviewFullConsumer(ctx)
    review_event = Event(
        event_id="evt-1",
        channel=CHANNEL_REVIEW_FULL,
        payload={"report_date": "20260730", "trace_id": "t2"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_review:
        mock_review.return_value = ReviewRunResult(
            status="ok",
            report_date="20260730",
            snapshot_kind="full",
            trace_id="t2",
            markdown="# Full",
        )
        await review_consumer.handle(review_event)

    # status=ok → review_done + snapshot(full) 两个发布
    channels = [c.args[0] for c in ctx.event_bus.publish.await_args_list]
    assert channels.count(CHANNEL_REVIEW_DONE) == 1
    assert channels.count(CHANNEL_SNAPSHOT) == 1
    snapshot_call = next(
        c for c in ctx.event_bus.publish.await_args_list if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_call.kwargs["payload"]["snapshot_kind"] == "full"

    # 2. SnapshotConsumer（full -> iterate）
    ctx.event_bus.publish.reset_mock()
    snap_consumer = SnapshotConsumer(ctx)
    snap_event = Event(
        event_id="evt-2",
        channel=CHANNEL_SNAPSHOT,
        payload={"report_date": "20260730", "snapshot_kind": "full"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.build_snapshot",
        return_value={"date": "20260730", "data": {}},
    ):
        await snap_consumer.handle(snap_event)

    ctx.event_bus.publish.assert_called_once_with(
        CHANNEL_ITERATE, payload={"report_date": "20260730"}
    )


@pytest.mark.asyncio
async def test_quick_review_skipped_when_full_report_exists(ctx):
    """覆盖逻辑：已有 full 报告时，quick review 跳过持久化。"""
    # mock node_api 返回已有 full 报告
    ctx.node_api.get_analysis_report = AsyncMock(return_value={
        "data_source": "review_agent_full",
        "content": {},
    })

    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-1",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "20260730", "trace_id": "t3"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_review:
        mock_review.return_value = ReviewRunResult(
            status="skipped",
            report_date="20260730",
            snapshot_kind="quick",
            trace_id="t3",
            markdown="",
        )
        await consumer.handle(event)

    # status=skipped 时仍 publish snapshot（quick snapshot 不依赖 review）
    ctx.event_bus.publish.assert_called_once()


@pytest.mark.asyncio
async def test_consumer_failure_triggers_retry(ctx):
    """consumer handle 失败时触发 retry（非 ack）。"""
    consumer = ReviewQuickConsumer(ctx)
    event = Event(
        event_id="evt-fail",
        channel=CHANNEL_REVIEW_QUICK,
        payload={"report_date": "20260730", "trace_id": "t4"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_review:
        mock_review.side_effect = Exception("LLM unavailable")
        # handle 失败应抛异常，由 consumer_loop 捕获后 retry
        with pytest.raises(Exception, match="LLM unavailable"):
            await consumer.handle(event)

    # retry 不在 handle 内调用，由 consumer_loop 负责
    # 此测试验证 handle 失败时抛异常（consumer_loop 会捕获并 retry）


@pytest.mark.asyncio
async def test_full_chain_review_done_to_prediction_save(ctx):
    """full 链路扩展：review_full(ok) -> review_done -> PredictionConsumer -> save_prediction 落库。

    预测数据源全部 mock（缓存/DB 重建/run_predict），验证接线与落库动作，
    不依赖真实 Redis/LLM/网络。
    """
    report_date = "2026-07-30"
    trace_id = "t9"

    # 1. ReviewFullConsumer status=ok → 发布 review_done（幂等 event_id）
    review_consumer = ReviewFullConsumer(ctx)
    review_event = Event(
        event_id="evt-r1",
        channel=CHANNEL_REVIEW_FULL,
        payload={"report_date": report_date, "trace_id": trace_id},
        group="evening_chain",
    )
    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_review:
        mock_review.return_value = ReviewRunResult(
            status="ok",
            report_date=report_date,
            snapshot_kind="full",
            trace_id=trace_id,
            markdown="# Full",
        )
        await review_consumer.handle(review_event)

    review_done_calls = [
        c for c in ctx.event_bus.publish.await_args_list if c.args[0] == CHANNEL_REVIEW_DONE
    ]
    assert len(review_done_calls) == 1
    assert review_done_calls[0].kwargs["event_id"] == f"review_done_{report_date}_{trace_id}"
    assert review_done_calls[0].kwargs["payload"] == {
        "report_date": report_date,
        "trace_id": trace_id,
    }

    # 2. PredictionConsumer 消费 review_done → predict_from_trace(ok) → save_prediction 落库
    snapshot_data = {
        "snapshot_id": "snap-t2",
        "trade_date": report_date,
        "captured_at": "2026-07-30T20:30:00",
        "a_share": {},
        "sources": {},
        "missing_fields": [],
        "phenomenon_discovery": {
            "status": "no_phenomenon",
            "primary": None,
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "complete",
                "attribution_inputs": "complete",
                "causal_evidence": "ready",
            },
            "diagnostics": [],
        },
    }
    trace_data = {
        "schema_version": "1.1",
        "attribution_status": "confirmed",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "medium",
        "unresolved_questions": [],
    }
    pred_result = PredictionRunResult(
        status="ok",
        prediction=PredictionResult(
            schema_version="2.0",
            prediction_status="hypothesis",
            horizons=[
                PredictionHorizon(
                    horizon="short",
                    remaining_estimate="2-4 周",
                    phase="building",
                    direction="bullish",
                    target="上证指数",
                    metric_projection="测试预期",
                    confidence="medium",
                )
            ],
            evolution_narrative="影响逐步减弱",
            risks=[],
            evidence_ids=[],
        ),
        due_dates={"short": "2026-08-06"},
    )
    fake_node = AsyncMock()
    fake_node.get_analysis_report = AsyncMock(
        return_value={"content": {"market_trace": {"snapshot": snapshot_data, "trace": trace_data}}}
    )
    fake_node.save_prediction = AsyncMock(return_value={"id": "pred-1"})

    with (
        patch(
            "aistock_agent.services.prediction_service.get_cached_review",
            new_callable=AsyncMock,
        ) as mock_cache,
        patch("aistock_agent.services.prediction_service.node_api", fake_node),
        patch(
            "aistock_agent.services.prediction_service.run_predict",
            new_callable=AsyncMock,
        ) as mock_run_predict,
    ):
        mock_cache.return_value = None
        mock_run_predict.return_value = pred_result

        pred_consumer = PredictionConsumer(ctx)
        review_done_event = Event(
            event_id=f"review_done_{report_date}_{trace_id}",
            channel=CHANNEL_REVIEW_DONE,
            payload={"report_date": report_date, "trace_id": trace_id},
            group="prediction_chain",
        )
        await pred_consumer.handle(review_done_event)

        # ok → save_prediction 恰好落库一次
        fake_node.save_prediction.assert_awaited_once()
        mock_run_predict.assert_awaited_once()
        # handle 不应触发任何事件总线操作（预测独立链路，不再下发事件）
        ctx.event_bus.retry.assert_not_called()
