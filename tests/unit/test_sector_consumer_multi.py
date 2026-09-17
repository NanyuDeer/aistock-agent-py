"""板块溯源多板块消费测试（spec P1a-2）。

覆盖 SectorTraceConsumer.handle 多板块并行语义（Task2）：
- extract_primary_sectors 命中 2 板块 → run_sector_trace 并行 awaited 2 次，各带相同
  parent_trace_ref（父链引用一致 → Task3 归因链可回溯同一大盘归因）；
- 单板块溯源失败仅 warning 不阻断其它板块，handle 不 raise（review_done 不进 retry/DLQ）；
- _review_index_pct 大盘指数涨跌候选键解析与降级（index_pct=None → relation unknown）。
"""
import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
        # 真实快照形状：a_share.indexes（list，每项含 name/code/change_pct）
        ({"indexes": [{"name": "上证指数", "code": "000001", "change_pct": -0.9}]}, -0.9),
        # 归一化形状：indexes 为 dict（key=SH000001），取上证而非首项
        (
            {
                "indexes": {
                    "SZ399001": {"ts_code": "399001.SZ", "change_pct": -1.8},
                    "SH000001": {"ts_code": "000001.SH", "change_pct": -0.9},
                }
            },
            -0.9,
        ),
        # indexes 存在但值非数值 → 回退旧键
        ({"indexes": [{"name": "上证指数", "change_pct": "x"}], "index_pct": 0.8}, 0.8),
    ],
)
def test_review_index_pct_candidate_keys(
    a_share: dict[str, object] | None, expected: float | None
) -> None:
    """_review_index_pct 候选键解析与缺失/畸形降级（含真实快照 indexes 键）。"""
    report = {"content": {"market_trace": {"snapshot": {"a_share": a_share}}}}
    assert _review_index_pct(report) == expected


@pytest.mark.asyncio
async def test_handle_parent_ref_index_pct_from_real_snapshot_indexes() -> None:
    """真实快照结构（a_share.indexes）→ parent_trace_ref.index_pct 非 None。"""
    ctx = object()
    consumer = SectorTraceConsumer(ctx=ctx)
    event = _make_event("2026-07-16")
    report = {
        "content": {
            "market_trace": {
                "snapshot": {
                    "a_share": {
                        "indexes": [
                            {"name": "上证指数", "code": "000001", "change_pct": -1.2}
                        ]
                    }
                }
            }
        }
    }
    with (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value=report),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=_TWO_SECTORS[:1],
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
    assert mock_run.await_args.kwargs["parent_trace_ref"]["index_pct"] == -1.2


# --- Task 1.1（spec §13.1 方案 A，P0' 时序）：链组装先于级联预判 + 两个 try 独立 ---


def _chain_store_patch(save_side_effect: object = None):
    """AttributionChainStore 替身（save=AsyncMock），供顺序/隔离断言。

    链保存在 handle 内是函数级 import，patch 模块属性即可对被调方生效。
    """
    store = SimpleNamespace(save=AsyncMock(side_effect=save_side_effect))
    return (
        patch(
            "aistock_agent.services.attribution_chain.AttributionChainStore",
            MagicMock(return_value=store),
        ),
        store,
    )


@contextmanager
def _sector_handle_patches(*, cascade: AsyncMock, store_patch: object) -> Iterator[None]:
    """handle 的常规依赖替身（review 报告 / 主因板块 / 溯源）+ 链 Store 替身。"""
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
        ),
        patch(
            "aistock_agent.services.event_consumers._cascade_sector_prediction",
            cascade,
        ),
        # Task 2.1：链事件层的中台事件池（避免测试真实打 Node 接口）
        patch(
            "aistock_agent.services.attribution_chain.load_chain_warehouse_events",
            AsyncMock(return_value=[]),
        ),
        store_patch,
    ):
        yield


@pytest.mark.asyncio
async def test_handle_saves_chain_before_cascade_prediction() -> None:
    """P0' 时序：链组装/保存完成后才触发级联预判（级联可读到当日链）。"""
    call_order: list[str] = []
    consumer = SectorTraceConsumer(ctx=object())
    event = _make_event("2026-07-16")

    async def _cascade(**kwargs: object) -> None:
        call_order.append("cascade")

    store_patch, _store = _chain_store_patch(lambda *a, **k: call_order.append("chain_save"))
    with _sector_handle_patches(
        cascade=AsyncMock(side_effect=_cascade), store_patch=store_patch
    ):
        await consumer.handle(event)

    assert call_order[0] == "chain_save"  # 链先于级联（旧时序此处为 cascade）
    assert call_order.count("chain_save") == 1
    assert call_order.count("cascade") == 2


@pytest.mark.asyncio
async def test_handle_cascade_runs_when_chain_save_fails() -> None:
    """链保存抛错 → 只 warning，不向外抛，且不得跳过级联预判。"""
    call_order: list[str] = []
    consumer = SectorTraceConsumer(ctx=object())
    event = _make_event("2026-07-16")

    async def _cascade(**kwargs: object) -> None:
        call_order.append("cascade")

    def _save_fail(*args: object, **kwargs: object) -> None:
        call_order.append("chain_save")
        raise RuntimeError("chain store down")

    store_patch, _store = _chain_store_patch(_save_fail)
    with _sector_handle_patches(
        cascade=AsyncMock(side_effect=_cascade), store_patch=store_patch
    ):
        await consumer.handle(event)  # 不得抛出（review_done 不进 retry/DLQ）

    assert call_order[0] == "chain_save"
    assert call_order.count("chain_save") == 1
    assert call_order.count("cascade") == 2


@pytest.mark.asyncio
async def test_handle_chain_result_kept_when_cascade_fails() -> None:
    """级联预判抛错 → 链已保存的结果不受影响，且 handle 不向外抛。"""
    call_order: list[str] = []
    consumer = SectorTraceConsumer(ctx=object())
    event = _make_event("2026-07-16")

    async def _cascade_fail(**kwargs: object) -> None:
        call_order.append("cascade")
        raise RuntimeError("cascade down")

    store_patch, store = _chain_store_patch(lambda *a, **k: call_order.append("chain_save"))
    with _sector_handle_patches(
        cascade=AsyncMock(side_effect=_cascade_fail), store_patch=store_patch
    ):
        await consumer.handle(event)  # 不得抛出

    assert store.save.await_count == 1  # 链已保存
    assert call_order[0] == "chain_save"
    assert call_order.count("cascade") == 2


@pytest.mark.asyncio
async def test_handle_passes_warehouse_events_to_chain_assembly() -> None:
    """Task 2.1 接线：链组装前读当日中台存量事件（一次读），并透传给组装做事件匹配。"""
    consumer = SectorTraceConsumer(ctx=object())
    event = _make_event("2026-07-16")
    warehouse = [
        {
            "event_id": "2026-07-16-abc123",
            "title": "半导体材料出口限制落地",
            "url": "https://news.example.com/a",
            "impact_score": 8,
        }
    ]
    store_patch, _store = _chain_store_patch()
    with (
        _sector_handle_patches(cascade=AsyncMock(return_value=None), store_patch=store_patch),
        patch(
            "aistock_agent.services.attribution_chain.load_chain_warehouse_events",
            AsyncMock(return_value=warehouse),
        ) as mock_load,
        patch(
            "aistock_agent.services.attribution_chain.assemble_attribution_chain",
            MagicMock(
                return_value={
                    "date": "2026-07-16",
                    "root": {"type": "market"},
                    "children": [],
                }
            ),
        ) as mock_assemble,
    ):
        await consumer.handle(event)

    mock_load.assert_awaited_once_with("2026-07-16")  # 每轮一次读（多板块共用同一事件池）
    assert mock_assemble.call_args.kwargs["warehouse_events"] == warehouse


@pytest.mark.asyncio
async def test_handle_cascade_predictions_run_concurrently() -> None:
    """级联预判必须并行触发（gather），不得串行线性累加 LLM 调用。"""
    in_flight = 0
    peak = 0
    consumer = SectorTraceConsumer(ctx=object())
    event = _make_event("2026-07-16")

    async def _cascade(**kwargs: object) -> None:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1

    store_patch, _store = _chain_store_patch()
    with _sector_handle_patches(
        cascade=AsyncMock(side_effect=_cascade), store_patch=store_patch
    ):
        await consumer.handle(event)

    assert peak == 2  # 两板块预判同时在飞（串行 for 会得到 1）
