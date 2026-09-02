"""SectorTraceConsumer 单元测试 — review_done(ok) → 板块溯源 + 级联预判（Spec D · T4/预判触发）。

patch 目标说明：event_consumers.py 顶部为
`from aistock_agent.agents.workers.sector_trace import extract_primary_sector, run_sector_trace`
与 `from aistock_agent.services.prediction_service import predict_sector`，
因此 handle() 内引用的名字位于 event_consumers 模块命名空间，patch 目标一律
指向 `aistock_agent.services.event_consumers.<name>`（命中实际引用点）。
"""

from types import SimpleNamespace
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


def _trace_result(*, sector: str = "存储板块") -> SimpleNamespace:
    """run_sector_trace 成功返回值（溯源快照含板块行情 + 来源，供级联预判）。"""
    return SimpleNamespace(
        snapshot={"sector": {"name": sector, "pct_change": -4.2}, "sources": []}
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
            AsyncMock(return_value=_trace_result()),
        ) as mock_run,
        patch(
            "aistock_agent.services.event_consumers.predict_sector",
            AsyncMock(return_value=None),
        ),
    ):
        await consumer.handle(event)
    mock_run.assert_awaited_once()
    assert consumer.channel == "review_done"
    assert consumer.consumer_group == "sector_chain"


@pytest.mark.asyncio
async def test_sector_trace_consumer_skips_when_no_primary_sector() -> None:
    """review 无主因板块 → 跳过不产出（不调 run_sector_trace、不触发级联预判）。"""
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
        patch(
            "aistock_agent.services.event_consumers.predict_sector",
            AsyncMock(return_value=None),
        ) as mock_predict,
    ):
        await consumer.handle(event)
    mock_run.assert_not_called()
    mock_predict.assert_not_awaited()


# --- Spec D 级联预判（预判环生产触发）：溯源成功 → predict_sector ---


@pytest.mark.asyncio
async def test_sector_trace_consumer_cascades_prediction_with_snapshot() -> None:
    """溯源成功 → 串行调 predict_sector，sector_snapshot 传溯源快照（预判生产触发）。"""
    ctx = object()
    consumer = SectorTraceConsumer(ctx=ctx)
    event = _make_event("2026-07-16")
    snapshot = _trace_result().snapshot
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
            AsyncMock(return_value=SimpleNamespace(snapshot=snapshot)),
        ),
        patch(
            "aistock_agent.services.event_consumers.predict_sector",
            AsyncMock(return_value=None),
        ) as mock_predict,
    ):
        await consumer.handle(event)
    mock_predict.assert_awaited_once_with(
        report_date="2026-07-16", sector_name="存储板块", sector_snapshot=snapshot
    )


@pytest.mark.asyncio
async def test_sector_trace_consumer_prediction_failure_does_not_raise() -> None:
    """级联预判抛异常/返回 None → 不阻断 handle（板块溯源事件不回 retry/DLQ）。"""
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
            AsyncMock(return_value=_trace_result()),
        ),
        patch(
            "aistock_agent.services.event_consumers.predict_sector",
            AsyncMock(side_effect=RuntimeError("resolve down")),
        ) as mock_predict,
    ):
        await consumer.handle(event)  # 不得抛异常
    mock_predict.assert_awaited_once()
