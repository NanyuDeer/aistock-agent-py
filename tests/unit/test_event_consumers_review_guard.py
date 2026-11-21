"""review 消费者：数据库无可追溯复盘时不得推进 review_done。"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.review import ReviewRunResult
from aistock_agent.services.event_bus import Event, EventBus
from aistock_agent.services.event_consumers import (
    CHANNEL_REVIEW_DONE,
    CHANNEL_REVIEW_FULL,
    CHANNEL_SNAPSHOT,
    ConsumerContext,
    ReviewFullConsumer,
    _has_traceable_review_report,
)


def _report(**overrides):
    base = {
        "id": 1,
        "report_type": "review",
        "status": "completed",
        "data_source": "review_agent_full",
        "content": {"display_report": {"summary": "ok"}},
    }
    base.update(overrides)
    return base


def test_traceable_review_requires_completed_and_content():
    assert _has_traceable_review_report(_report())
    assert not _has_traceable_review_report(None)
    assert not _has_traceable_review_report(_report(status="failed"))
    assert not _has_traceable_review_report(_report(content={}))
    assert not _has_traceable_review_report(_report(data_source=""))
    assert not _has_traceable_review_report(_report(id=0))


@pytest.mark.asyncio
async def test_full_consumer_skips_review_done_when_db_missing():
    """run_review 返回 ok 但 DB 读不到复盘 → 不发 review_done，snapshot 标降级。"""
    bus = AsyncMock(spec=EventBus)
    bus.publish = AsyncMock(return_value="evt-new")
    api = AsyncMock()
    api.get_analysis_report_quiet = AsyncMock(return_value=None)
    ctx = ConsumerContext(bus, api)
    consumer = ReviewFullConsumer(ctx)
    event = Event(
        event_id="evt-missing",
        channel=CHANNEL_REVIEW_FULL,
        payload={"report_date": "2026-11-20", "trace_id": "t1"},
        group="evening_chain",
    )

    with patch(
        "aistock_agent.services.event_consumers.run_review",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = ReviewRunResult(
            status="ok",
            report_date="2026-11-20",
            snapshot_kind="full",
            trace_id="t1",
            markdown="# Full",
        )
        await consumer.handle(event)

    channels = [c.args[0] for c in bus.publish.await_args_list]
    assert CHANNEL_REVIEW_DONE not in channels
    assert CHANNEL_SNAPSHOT in channels
    snapshot_payload = next(
        c.kwargs["payload"]
        for c in bus.publish.await_args_list
        if c.args[0] == CHANNEL_SNAPSHOT
    )
    assert snapshot_payload["review_degraded"] is True
    assert snapshot_payload["review_status"] == "degraded"
