"""SectorTraceConsumer 单元测试 — review_done(ok) → 板块溯源 + 级联预判（Spec D · T4/预判触发）。

patch 目标说明：event_consumers.py 顶部为
`from aistock_agent.agents.workers.sector_trace import extract_primary_sectors, run_sector_trace`
与 `from aistock_agent.services.prediction_service import predict_sector`，
因此 handle() 内引用的名字位于 event_consumers 模块命名空间，patch 目标一律
指向 `aistock_agent.services.event_consumers.<name>`（命中实际引用点）。

Task2 多板块语义：extract_primary_sectors 返回板块提取命中项列表
`list[SectorHit]`（无命中返回 []），handle 逐板块并行溯源。
Task 9.1 三级兜底：兜底命中（candidate_claim/snapshot）仍照常溯源 + 级联预判，
但携带弱依据标记（链组装 children[].extraction / 预判留痕 attribution_weak）。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.agents.workers.sector_trace import (
    SOURCE_CANDIDATE_CLAIM,
    SOURCE_PRIMARY_CLAIM,
    SOURCE_SNAPSHOT,
    SectorHit,
)
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


def _hit(
    name: str = "存储板块",
    row: dict[str, object] | None = None,
    source: str = SOURCE_PRIMARY_CLAIM,
) -> SectorHit:
    return SectorHit(name=name, row=row or {"name": name, "pct_change": -4.2}, source=source)


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
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=[_hit()],
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
    """review 无主因板块（三级兜底亦无产出）→ 跳过不产出、不触发级联预判。"""
    ctx = object()
    consumer = SectorTraceConsumer(ctx=ctx)
    event = _make_event("2026-07-16")
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value={}),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=[],
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
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=[_hit()],
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
    # 主链命中（T1）→ 非弱依据，级联预判留痕不点亮弱标记
    mock_predict.assert_awaited_once_with(
        report_date="2026-07-16",
        sector_name="存储板块",
        sector_snapshot=snapshot,
        attribution_weak=False,
        extraction_source=SOURCE_PRIMARY_CLAIM,
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
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=[_hit()],
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


# --- Task 9.1：弱依据兜底（candidate_claim/snapshot）不得中断链路，但须标注 ---


@pytest.mark.parametrize("source", [SOURCE_CANDIDATE_CLAIM, SOURCE_SNAPSHOT])
@pytest.mark.asyncio
async def test_sector_trace_consumer_keeps_weak_fallback_with_marks(
    source: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """兜底命中（T2/T3）→ 照常溯源 + 级联预判，且链组装/预判留痕带弱依据标记。"""
    consumer = SectorTraceConsumer(ctx=object())
    event = _make_event("2026-09-17")
    trace_result = _trace_result(sector="金属铅")
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value={}),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=[_hit("金属铅", source=source)],
        ),
        patch(
            "aistock_agent.services.event_consumers.run_sector_trace",
            AsyncMock(return_value=trace_result),
        ) as mock_run,
        patch(
            "aistock_agent.services.event_consumers.predict_sector",
            AsyncMock(return_value=None),
        ) as mock_predict,
        patch(
            "aistock_agent.services.attribution_chain.load_chain_warehouse_events",
            AsyncMock(return_value=[]),
        ),
        patch(
            "aistock_agent.services.attribution_chain.assemble_attribution_chain",
            MagicMock(return_value={"date": "2026-09-17", "root": {}, "children": []}),
        ) as mock_assemble,
        patch(
            "aistock_agent.services.attribution_chain.AttributionChainStore",
            MagicMock(return_value=SimpleNamespace(save=AsyncMock(return_value=None))),
        ),
    ):
        await consumer.handle(event)

    # 兜底不得跳过溯源与级联预判
    mock_run.assert_awaited_once()
    mock_predict.assert_awaited_once()
    # 来源/弱信息随溯源结果传给链组装（children[].extraction 的输入）
    results = mock_assemble.call_args.args[2]
    assert results[0].extraction == {"source": source, "weak": True}
    # 级联预判留痕带弱依据标记
    assert mock_predict.await_args.kwargs["attribution_weak"] is True
    assert mock_predict.await_args.kwargs["extraction_source"] == source
    # 观测：done 日志带来源（日志渲染形态随环境不同：JSON / console，逐行匹配）
    done_lines = [
        line for line in capsys.readouterr().out.splitlines() if "sector_trace_done" in line
    ]
    assert done_lines
    assert any("extraction_source" in line and source in line for line in done_lines)


@pytest.mark.asyncio
async def test_sector_trace_consumer_primary_hit_carries_no_weak_mark() -> None:
    """主链命中（T1）→ 溯源结果来源 primary_claim 且 weak=False（不污染正常链）。"""
    consumer = SectorTraceConsumer(ctx=object())
    event = _make_event("2026-09-17")
    trace_result = _trace_result()
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value={}),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=[_hit()],
        ),
        patch(
            "aistock_agent.services.event_consumers.run_sector_trace",
            AsyncMock(return_value=trace_result),
        ),
        patch(
            "aistock_agent.services.event_consumers.predict_sector",
            AsyncMock(return_value=None),
        ),
        patch(
            "aistock_agent.services.attribution_chain.load_chain_warehouse_events",
            AsyncMock(return_value=[]),
        ),
        patch(
            "aistock_agent.services.attribution_chain.assemble_attribution_chain",
            MagicMock(return_value={"date": "2026-09-17", "root": {}, "children": []}),
        ),
        patch(
            "aistock_agent.services.attribution_chain.AttributionChainStore",
            MagicMock(return_value=SimpleNamespace(save=AsyncMock(return_value=None))),
        ),
    ):
        await consumer.handle(event)
    assert trace_result.extraction == {"source": SOURCE_PRIMARY_CLAIM, "weak": False}

