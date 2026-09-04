"""板块溯源多板块消费测试（spec P1a-2）。

覆盖 SectorTraceConsumer.handle 多板块并行语义（Task2）：
- extract_primary_sectors 命中 2 板块 → run_sector_trace 并行 awaited 2 次，各带相同
  parent_trace_ref（父链引用一致 → Task3 归因链可回溯同一大盘归因）；
- 单板块溯源失败仅 warning 不阻断其它板块，handle 不 raise（review_done 不进 retry/DLQ）；
- _review_index_pct 大盘指数涨跌候选键解析与降级（index_pct=None → relation unknown）。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation
from aistock_agent.services.event_consumers import SectorTraceConsumer, _review_index_pct


@pytest.mark.parametrize(
    "sector_pct,index_pct,expected",
    [
        (3.0, -0.5, "self_driven"),
        (-2.0, 0.5, "self_driven"),
        (3.0, 1.0, "self_driven"),
        (0.8, 1.0, "market_follow"),
        (None, 1.0, "unknown"),
    ],
)
def test_judge_sector_driver_relation(sector_pct, index_pct, expected):
    assert judge_sector_driver_relation(sector_pct, index_pct) == expected


# --- SectorTraceConsumer.handle 多板块并行语义（Task2 回归缺口） ---


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


def _review_report(*, index_pct: float | None) -> dict:
    """node_api.get_analysis_report 返回的 review 报告（a_share 含大盘指数涨跌）。"""
    a_share: dict[str, object] = {}
    if index_pct is not None:
        a_share = {"index_change_pct": index_pct}
    return {"content": {"market_trace": {"snapshot": {"a_share": a_share}}}}


_TWO_SECTORS = [("存储板块", {"pct_change": -4.2}), ("券商板块", {"pct_change": -2.1})]
_PARENT_REF = {"source_report_type": "review", "report_date": "2026-07-16", "index_pct": -1.2}


@pytest.mark.asyncio
async def test_handle_parallel_traces_two_sectors_with_same_parent_ref() -> None:
    """2 主因板块 → run_sector_trace 并行 awaited 2 次，各带相同 parent_trace_ref。"""
    ctx = object()
    consumer = SectorTraceConsumer(ctx=ctx)
    event = _make_event("2026-07-16")
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value=_review_report(index_pct=-1.2)),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=_TWO_SECTORS,
        ),
        patch(
            "aistock_agent.services.event_consumers.run_sector_trace",
            AsyncMock(return_value=SimpleNamespace(snapshot={})),
        ) as mock_run,
        patch(
            "aistock_agent.services.event_consumers._cascade_sector_prediction",
            AsyncMock(return_value=None),
        ),
    ):
        await consumer.handle(event)
    assert mock_run.await_count == 2
    for call in mock_run.await_args_list:
        assert call.kwargs["parent_trace_ref"] == _PARENT_REF


@pytest.mark.asyncio
async def test_handle_one_sector_failure_does_not_block_others() -> None:
    """首板块溯源抛错 → handle 不 raise，次板块仍完成（含级联预判）。"""
    ctx = object()
    consumer = SectorTraceConsumer(ctx=ctx)
    event = _make_event("2026-07-16")
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value=_review_report(index_pct=-1.2)),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=_TWO_SECTORS,
        ),
        patch(
            "aistock_agent.services.event_consumers.run_sector_trace",
            AsyncMock(
                side_effect=[RuntimeError("trace down"), SimpleNamespace(snapshot={})]
            ),
        ) as mock_run,
        patch(
            "aistock_agent.services.event_consumers._cascade_sector_prediction",
            AsyncMock(return_value=None),
        ) as mock_cascade,
    ):
        await consumer.handle(event)  # 不得抛异常（review_done 不进 retry/DLQ）
    assert mock_run.await_count == 2
    assert mock_cascade.await_count == 1
    assert mock_cascade.await_args.kwargs["sector_name"] == "券商板块"


@pytest.mark.parametrize(
    "a_share,expected",
    [
        ({"index_change_pct": -1.2}, -1.2),
        ({"index_pct": 0.8}, 0.8),
        ({"benchmark_change_pct": 1.5}, 1.5),
        ({"sh_change_pct": -0.3}, -0.3),
        # 候选键顺序优先：index_change_pct 在前
        ({"index_change_pct": -1.2, "sh_change_pct": 2.0}, -1.2),
        ({}, None),
        (None, None),
        ({"index_change_pct": "0.5"}, None),  # 非数值类型 → None（降级 unknown）
    ],
)
def test_review_index_pct_candidate_keys(
    a_share: dict[str, object] | None, expected: float | None
) -> None:
    """_review_index_pct 四候选键解析与缺失/畸形降级。"""
    report = {"content": {"market_trace": {"snapshot": {"a_share": a_share}}}}
    assert _review_index_pct(report) == expected
