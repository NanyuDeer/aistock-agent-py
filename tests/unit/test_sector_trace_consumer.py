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

from contextlib import ExitStack
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
    # R14：命中的快照行随结果携带（链组装 children[].ts_code/sector_std 的输入）
    assert results[0].sector_row == {"name": "金属铅", "pct_change": -4.2}
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


# --- 兜底板块补跑板块溯源（2026-09-18 组长口径）---
#
# 口径：T2/T3 兜底命中的板块**必须逐个真跑 run_sector_trace**（不是只把名字写进链）——
# 弱归因日若只点名不溯源，链 children 的 trace_summary 只能落中性兜底、前端按
# 「未确认不显示」过滤后当日内容近乎为空。上限 max_sectors=3，超出截断并留痕；
# T1（有主链）路径逐字不变（不触发补跑日志）。

_FALLBACK_MAX = 3

# 弱归因日 review 报告（无主链、候选全 weak）：仅用于解析大盘涨跌幅（index_pct=-0.9）
_FALLBACK_REVIEW_REPORT: dict[str, object] = {
    "content": {
        "market_trace": {
            "snapshot": {"a_share": {"indexes": [{"name": "上证指数", "change_pct": -0.9}]}},
            "trace": {"primary_chain_id": None, "attribution_summary": "", "candidates": []},
        }
    }
}


def _fallback_hit(name: str, source: str = SOURCE_CANDIDATE_CLAIM) -> SectorHit:
    return SectorHit(name=name, row={"name": name, "pct_change": -4.0}, source=source)


def _fallback_trace_result(name: str) -> SimpleNamespace:
    """兜底板块溯源结果：带 trigger 段（真实归因句 → 链 trace_summary 非中性兜底）。"""
    return SimpleNamespace(
        sector=name,
        snapshot={"sector": {"name": name, "pct_change": -4.0}, "sources": []},
        trace_result={
            "chain_id": f"sc-{name}",
            "sector": name,
            "attribution_status": "sufficient",
            "stages": [
                {
                    "kind": "phenomenon",
                    "headline": f"{name}放量调整",
                    "claims": [],
                    "evidence": [],
                },
                {
                    "kind": "trigger",
                    "headline": f"{name}受出口管制预期压制",
                    "claims": [],
                    "evidence": [],
                },
            ],
        },
    )


def _fallback_stack(
    hits: list[SectorHit], trace: object
) -> tuple[tuple[object, ...], AsyncMock, SimpleNamespace]:
    """兜底补跑用例的公共打桩（真实 assemble + Store 替身，便于断言链 children）。"""
    run = AsyncMock(side_effect=trace)
    store = SimpleNamespace(save=AsyncMock(return_value=None))
    patches = (
        patch(
            "aistock_agent.services.event_consumers.node_api.get_analysis_report",
            AsyncMock(return_value=_FALLBACK_REVIEW_REPORT),
        ),
        patch(
            "aistock_agent.services.event_consumers.extract_primary_sectors",
            return_value=hits,
        ),
        patch("aistock_agent.services.event_consumers.run_sector_trace", run),
        patch(
            "aistock_agent.services.event_consumers.predict_sector",
            AsyncMock(return_value=None),
        ),
        patch(
            "aistock_agent.services.attribution_chain.load_chain_warehouse_events",
            AsyncMock(return_value=[]),
        ),
        patch(
            "aistock_agent.services.attribution_chain.AttributionChainStore",
            MagicMock(return_value=store),
        ),
    )
    return patches, run, store


async def _handle_with(patches: tuple[object, ...], report_date: str) -> None:
    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)  # type: ignore[arg-type]
        await SectorTraceConsumer(ctx=object()).handle(_make_event(report_date))


@pytest.mark.asyncio
async def test_fallback_t2_traces_each_hit_and_chain_children_get_real_summary() -> None:
    """T2 兜底命中 3 板块 → 3 个都真跑溯源，链 children 带真实 trace_summary（非中性兜底）。"""
    hits = [_fallback_hit("CRO概念"), _fallback_hit("转基因"), _fallback_hit("玉米")]
    patches, run, store = _fallback_stack(
        hits, lambda **kw: _fallback_trace_result(str(kw["sector_name"]))
    )
    await _handle_with(patches, "2026-09-17")

    assert run.await_count == _FALLBACK_MAX
    assert [c.kwargs["sector_name"] for c in run.await_args_list] == [
        "CRO概念",
        "转基因",
        "玉米",
    ]
    chain = store.save.await_args.args[1]
    children = chain["children"]
    assert [c["sector"] for c in children] == ["CRO概念", "转基因", "玉米"]
    # 真实归因句（trigger headline）而非中性兜底文案
    assert [c["trace_summary"] for c in children] == [
        "CRO概念受出口管制预期压制",
        "转基因受出口管制预期压制",
        "玉米受出口管制预期压制",
    ]
    assert all(c["trace_summary"] != "溯源未确认驱动原因" for c in children)
    # 回归：弱依据标注语义不变
    assert all(
        c["extraction"] == {"source": SOURCE_CANDIDATE_CLAIM, "weak": True}
        for c in children
    )
    assert chain["root"]["evidence_weak"] is True


@pytest.mark.asyncio
async def test_fallback_t3_traces_snapshot_hits_with_level_log(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T2 无命中降级 T3（快照桶）→ 同样逐个补跑，日志带来源级别 T3。"""
    hits = [_fallback_hit("金属铅", SOURCE_SNAPSHOT), _fallback_hit("玉米", SOURCE_SNAPSHOT)]
    patches, run, store = _fallback_stack(
        hits, lambda **kw: _fallback_trace_result(str(kw["sector_name"]))
    )
    await _handle_with(patches, "2026-09-17")

    assert [c.kwargs["sector_name"] for c in run.await_args_list] == ["金属铅", "玉米"]
    chain = store.save.await_args.args[1]
    assert [c["extraction"] for c in chain["children"]] == [
        {"source": SOURCE_SNAPSHOT, "weak": True},
        {"source": SOURCE_SNAPSHOT, "weak": True},
    ]
    assert chain["root"]["evidence_weak"] is True
    out = capsys.readouterr().out
    assert "sector_trace_fallback_started" in out
    assert "T3" in out


@pytest.mark.asyncio
async def test_fallback_over_cap_truncates_and_logs(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """兜底命中超过上限（5 个）→ 只补跑前 max_sectors（3）个 + 截断留痕。"""
    names = ["A板块", "B板块", "C板块", "D板块", "E板块"]
    hits = [_fallback_hit(n, SOURCE_SNAPSHOT) for n in names]
    patches, run, store = _fallback_stack(
        hits, lambda **kw: _fallback_trace_result(str(kw["sector_name"]))
    )
    await _handle_with(patches, "2026-09-17")

    assert run.await_count == _FALLBACK_MAX
    assert [c.kwargs["sector_name"] for c in run.await_args_list] == names[:_FALLBACK_MAX]
    chain = store.save.await_args.args[1]
    assert [c["sector"] for c in chain["children"]] == names[:_FALLBACK_MAX]
    assert "sector_trace_fallback_truncated" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_fallback_one_failure_keeps_others_and_chain_save(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """补跑中 1 个板块抛异常 → 该板块如实未确认，其余板块与链保存不受影响。"""
    hits = [_fallback_hit("CRO概念"), _fallback_hit("转基因"), _fallback_hit("玉米")]

    def _trace(**kw: object) -> SimpleNamespace:
        name = str(kw["sector_name"])
        if name == "转基因":
            raise RuntimeError("tavily down")
        return _fallback_trace_result(name)

    patches, run, store = _fallback_stack(hits, _trace)
    await _handle_with(patches, "2026-09-17")

    assert run.await_count == 3  # 逐项独立：失败不阻止其它板块被调用
    chain = store.save.await_args.args[1]
    assert [c["sector"] for c in chain["children"]] == ["CRO概念", "玉米"]
    assert store.save.await_count == 1  # 链仍保存成功
    out = capsys.readouterr().out
    assert "sector_trace_fallback_failed" in out


@pytest.mark.asyncio
async def test_primary_hit_path_does_not_trigger_fallback_pass(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """T1 主链命中 → 不触发兜底补跑（调用次数/入参逐字不变，无兜底日志）。"""
    patches, run, store = _fallback_stack(
        [_hit()], lambda **kw: _fallback_trace_result(str(kw["sector_name"]))
    )
    await _handle_with(patches, "2026-09-17")

    assert run.await_args.kwargs == {
        "report_date": "2026-09-17",
        "sector_name": "存储板块",
        "sector_row": {"name": "存储板块", "pct_change": -4.2},
        "parent_trace_ref": {
            "source_report_type": "review",
            "report_date": "2026-09-17",
            "index_pct": -0.9,
        },
    }
    out = capsys.readouterr().out
    assert "sector_trace_fallback_started" not in out
    # 正常链不被弱标记污染
    chain = store.save.await_args.args[1]
    assert "extraction" not in chain["children"][0]
    assert "evidence_weak" not in chain["root"]

