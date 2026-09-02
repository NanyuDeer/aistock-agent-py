"""SectorTraceConsumer 单元测试 — review_done(ok) → 板块溯源（Spec D · T4）。

patch 目标说明：event_consumers.py 顶部为
`from aistock_agent.agents.workers.sector_trace import extract_primary_sector, run_sector_trace`，
因此 handle() 内引用的名字位于 event_consumers 模块命名空间，patch 目标一律
指向 `aistock_agent.services.event_consumers.<name>`（命中实际引用点）。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_consumers import SectorTraceConsumer


def _make_event(report_date: str) -> object:
    return type(
        "Ev",
        (),
        {
            "event_id": f"review_done_{report_date}_x",
            "payload": {"report_date": report_date},
            "group": "sector_chain",
        },
    )


@pytest.mark.asyncio
async def test_sector_trace_consumer_consumes_review_done() -> None:
    """review_done(ok) → 消费 → 调 run_sector_trace（主因板块命中路径）。"""
    ctx = object()
    consumer = SectorTraceConsumer(ctx=ctx)
    event = _make_event("2026-07-16")
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value={}),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sector",
            return_value=("存储板块", {"pct_change": -4.2}),
        ),
        patch(
            "aistock_agent.services.event_consumers.run_sector_trace",
            AsyncMock(return_value=None),
        ) as mock_run,
    ):
        await consumer.handle(event)
    mock_run.assert_awaited_once()
    assert consumer.channel == "review_done"
    assert consumer.consumer_group == "sector_chain"


@pytest.mark.asyncio
async def test_sector_trace_consumer_skips_when_no_primary_sector() -> None:
    """review 无主因板块 → 跳过不产出（不调 run_sector_trace）。"""
    ctx = object()
    consumer = SectorTraceConsumer(ctx=ctx)
    event = _make_event("2026-07-16")
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value={}),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sector",
            return_value=(None, None),
        ),
        patch(
            "aistock_agent.services.event_consumers.run_sector_trace",
            AsyncMock(return_value=None),
        ) as mock_run,
    ):
        await consumer.handle(event)
    mock_run.assert_not_called()
