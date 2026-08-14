"""case_sourcers 注册表清单封闭 + provider 候选构造（二期 case-sourcing）。"""

import asyncio
from datetime import date as _date
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.adapters import get_adapter
from aistock_agent.iterate.case_sourcers import SOURCE_PROVIDERS


def test_registry_closed_over_adapter_references() -> None:
    # 清单封闭：adapter 引用的 provider 名必须全部已登记
    for adapter in (get_adapter("review"), get_adapter("event_analyst")):
        for spec in adapter.case_sources:
            assert spec.provider in SOURCE_PROVIDERS, f"{spec.provider} 未登记"


def test_telegraph_scan_provider_maps_candidates() -> None:
    from aistock_agent.iterate.case_sourcers import SourceContext, telegraph_keyword_scan

    ctx = SourceContext(agent_id="event_analyst", params={"window_days": 30}, data_dir=None)
    scanner_result = [
        {
            "event_title": "央行降准",
            "event_time": "2026-08-01T10:30:00+08:00",
            "telegraph_records": [{"time": "2026-08-01T10:00:00+08:00", "title": "央行宣布降准"}],
        }
    ]
    with patch(
        "aistock_agent.iterate.case_scanner.scan_major_events",
        AsyncMock(return_value=scanner_result),
    ):
        candidates = asyncio.run(telegraph_keyword_scan(ctx))
    assert len(candidates) == 1
    assert candidates[0].event_title == "央行降准"
    assert candidates[0].meta == {"t_window": "event"}


def test_source_cases_skips_failed_provider() -> None:
    from types import SimpleNamespace

    from aistock_agent.iterate.case_sourcers import source_cases

    async def boom(ctx: object) -> list[object]:
        raise RuntimeError("provider boom")

    fake_adapter = SimpleNamespace(
        agent_id="x", case_sources=[SimpleNamespace(provider="boom", params={})]
    )
    with patch("aistock_agent.iterate.case_sourcers.SOURCE_PROVIDERS", {"boom": boom}):
        results = asyncio.run(source_cases(fake_adapter))  # type: ignore[arg-type]
    assert results == []


def test_market_close_snapshot_uses_date_param_when_provided() -> None:
    """三期：provider params 带 date → 历史回补分支（build_market_trace_snapshot 收到指定日期）。"""
    import asyncio

    from aistock_agent.iterate.case_sourcers import SourceContext, market_close_snapshot

    ctx = SourceContext(agent_id="review", params={"date": "2026-08-07"}, data_dir=None)
    # provider 需要：trade_date/captured_at/phenomenon_discovery 属性 + model_dump(mode="json")
    # model_dump 必须返回含 a_share.indexes 的完整快照（否则 _snapshot_data_sufficient 拒绝产片）
    snapshot_dict = {
        "trade_date": "2026-08-07",
        "captured_at": "2026-08-07T08:00:00+00:00",
        "a_share": {"indexes": {"000001": {"change_pct": 1.2}}},
        "sources": {},
        "missing_fields": [],
    }
    fake_snapshot = SimpleNamespace(
        trade_date="2026-08-07",
        captured_at=datetime(2026, 8, 7, 8, 0, tzinfo=timezone.utc),  # noqa: UP017
        phenomenon_discovery=SimpleNamespace(primary=SimpleNamespace(summary="A股收盘2026-08-07")),
    )
    fake_snapshot.model_dump = lambda mode="python", **kw: snapshot_dict  # type: ignore[attr-defined]

    with (
        patch("aistock_agent.iterate.case_sourcers.find_recent_trading_day", AsyncMock(return_value="2026-08-06")),  # noqa: E501
        patch("aistock_agent.iterate.case_sourcers.build_market_trace_snapshot", AsyncMock(return_value=fake_snapshot)) as mock_build,  # noqa: E501
        patch("aistock_agent.iterate.case_sourcers._collect_industry_graph", AsyncMock(return_value=None)),  # noqa: E501
    ):
        candidates = asyncio.run(market_close_snapshot(ctx))
    # 历史分支：不调用 find_recent_trading_day，直接以 date 构建
    mock_build.assert_awaited_once_with("2026-08-07")
    assert len(candidates) == 1
    assert candidates[0].event_title == "A股收盘2026-08-07"
    # 三期实测修复：历史回补 case 的 event_time 锚定目标交易日 15:30 CST（UTC 07:30），
    # 而非构建时刻 captured_at——否则 case_id 前缀/ T 窗口锚定错标为构建日
    assert candidates[0].event_time == datetime(2026, 8, 7, 7, 30, tzinfo=timezone.utc)  # noqa: UP017


def test_market_close_snapshot_no_date_uses_recent_trading_day() -> None:
    """回归：无 date 走 find_recent_trading_day（二期行为不变）。"""
    import asyncio

    from aistock_agent.iterate.case_sourcers import SourceContext, market_close_snapshot

    ctx = SourceContext(agent_id="review", params={}, data_dir=None)
    snapshot_dict = {
        "trade_date": "2026-08-14",
        "captured_at": "2026-08-14T08:00:00+00:00",
        "a_share": {"indexes": {"000001": {"change_pct": 0.8}}},
        "sources": {},
        "missing_fields": [],
    }
    fake_snapshot = SimpleNamespace(
        trade_date="2026-08-14",
        captured_at=datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc),  # noqa: UP017
        phenomenon_discovery=SimpleNamespace(primary=SimpleNamespace(summary="A股收盘2026-08-14")),
    )
    fake_snapshot.model_dump = lambda mode="python", **kw: snapshot_dict  # type: ignore[attr-defined]

    with (
        patch("aistock_agent.iterate.case_sourcers.find_recent_trading_day", AsyncMock(return_value="2026-08-14")) as mock_find,  # noqa: E501
        patch("aistock_agent.iterate.case_sourcers.build_market_trace_snapshot", AsyncMock(return_value=fake_snapshot)) as mock_build,  # noqa: E501
        patch("aistock_agent.iterate.case_sourcers._collect_industry_graph", AsyncMock(return_value=None)),  # noqa: E501
    ):
        candidates = asyncio.run(market_close_snapshot(ctx))
    mock_find.assert_awaited_once()
    mock_build.assert_awaited_once_with("2026-08-14")
    assert len(candidates) == 1


def test_market_close_snapshot_date_param_rejects_trade_date_mismatch() -> None:
    """三期评审（IMP-3）：date 分支回补后 trade_date 与请求日期不一致（非交易日/数据缺失被
    last-close 兜底产出"最近交易日"）→ provider 必须抛 RuntimeError，拒绝产片。

    硬约束：回补失败 → provider 抛错 → source_cases 降级 0 候选；
    不得静默产出 trade_date 与请求 date 不一致的 case。
    """
    from aistock_agent.iterate.case_sourcers import SourceContext, market_close_snapshot

    ctx = SourceContext(agent_id="review", params={"date": "2026-08-07"}, data_dir=None)
    # build_market_trace_snapshot 走 last-close 兜底：实际 trade_date 是最近交易日（非 2026-08-07）
    fake_snapshot = SimpleNamespace(trade_date="2026-08-06")

    with (
        patch("aistock_agent.iterate.case_sourcers.find_recent_trading_day", AsyncMock(return_value="2026-08-06")),  # noqa: E501
        patch("aistock_agent.iterate.case_sourcers.build_market_trace_snapshot", AsyncMock(return_value=fake_snapshot)),  # noqa: E501
    ):
        with pytest.raises(RuntimeError, match="历史回补日期不一致"):
            asyncio.run(market_close_snapshot(ctx))


def test_event_store_scan_filters_major_events() -> None:
    """四期：事件库产片源——is_major_event 过滤 + 候选构造（meta 带 direction_hint）。"""
    import asyncio

    from aistock_agent.iterate.case_sourcers import SourceContext, event_store_scan

    ctx = SourceContext(agent_id="event_analyst", params={"window_days": 1}, data_dir=None)
    major = {
        "title": "央行降准 50 基点", "summary": "央行宣布降准支持实体经济",
        "url": "https://x/1", "impact_score": 5, "direction": "bullish",
        "source": "cls", "score_date": "2026-08-14 10:30:00+08:00", "scrape_at": "2026-08-14T10:00:00+08:00",  # noqa: E501
    }
    minor = {
        "title": "某公司发布公告", "summary": "常规公告", "url": "https://x/2",
        "impact_score": 2, "direction": "neutral", "source": "cls",
        "score_date": "2026-08-14 10:30:00+08:00", "scrape_at": "2026-08-14T10:00:00+08:00",
    }
    with (
        patch("aistock_agent.iterate.case_sourcers.load_event_scrape", AsyncMock(return_value=[major, minor])),  # noqa: E501
        patch("aistock_agent.iterate.case_sourcers.shanghai_today", return_value=_date(2026, 8, 14)),  # noqa: E501
    ):
        candidates = asyncio.run(event_store_scan(ctx))
    assert len(candidates) == 1  # 仅 major
    assert candidates[0].event_title == "央行降准 50 基点"
    assert candidates[0].telegraph_records[0]["content"] == "央行宣布降准支持实体经济"
    assert candidates[0].meta == {
        "t_window": "event", "source": "event_store", "direction_hint": "bullish",
    }


def test_event_store_scan_skips_failed_day() -> None:
    """四期：单日事件库读取失败降级跳过（不阻断其他天）。"""
    import asyncio

    from aistock_agent.iterate.case_sourcers import SourceContext, event_store_scan

    ctx = SourceContext(agent_id="event_analyst", params={"window_days": 2}, data_dir=None)

    async def flaky(score_date: str):
        if score_date == "2026-08-14":
            raise RuntimeError("db timeout")
        return []

    with (
        patch("aistock_agent.iterate.case_sourcers.load_event_scrape", side_effect=flaky),
        patch("aistock_agent.iterate.case_sourcers.shanghai_today", return_value=_date(2026, 8, 14)),  # noqa: E501
    ):
        candidates = asyncio.run(event_store_scan(ctx))
    assert candidates == []  # 失败日跳过，不抛


def test_event_analyst_registers_event_store_first() -> None:
    """四期：event_analyst 产片源 = 事件库主源 + 电报后备（事件库在前保证去重优先）。"""
    from aistock_agent.iterate.adapters import get_adapter

    adapter = get_adapter("event_analyst")
    assert [s.provider for s in adapter.case_sources] == [
        "event_store_scan", "telegraph_keyword_scan",
    ]


def test_candidate_fingerprint_normalizes_title() -> None:
    """四期：标题归一化指纹——空白/标点差异视为同事件。"""
    from aistock_agent.iterate.case_sourcers import (  # noqa: PLC2701
        CaseCandidate,
        _candidate_fingerprint,
    )

    mk = lambda t: CaseCandidate(  # noqa: E731
        event_title=t, event_time=datetime(2026, 8, 14, 2, 30, tzinfo=timezone.utc),  # noqa: UP017
        telegraph_records=[], meta=None,
    )
    assert _candidate_fingerprint(mk("央行降准 50 基点")) == _candidate_fingerprint(
        mk("央行降准50基点！")
    )


def test_source_cases_dedupes_same_event_across_sources() -> None:
    """四期：两源同事件（同标题指纹）→ 仅 1 候选（首个保留 = 事件库优先）；不同事件 → 全部保留。

    注（brief 测试适配）：source_cases 对全部 provider 传同一 adapter.agent_id，
    brief 原文用 ctx.agent_id == "store" 判别两源恒不成立（两源都会返回 tele_c，
    去重后仅 1 候选 → 断言必失败）。按最小改动改为按 spec.params 判别（source_cases
    透传 spec.params 进 SourceContext），测试意图不变。
    """
    import asyncio

    from aistock_agent.iterate.adapters import IterableAgentAdapter
    from aistock_agent.iterate.case_sourcers import CaseCandidate, SourceContext, source_cases

    mk = lambda title, src: CaseCandidate(  # noqa: E731
        event_title=title, event_time=datetime(2026, 8, 14, 2, 30, tzinfo=timezone.utc),  # noqa: UP017
        telegraph_records=[{"time": "t", "title": title, "content": "c", "url": "u"}],
        meta={"source": src},
    )
    store_c = mk("央行降准 50 基点", "event_store")
    tele_c = mk("央行降准50基点！", "telegraph")   # 同指纹
    other_c = mk("美联储加息 25 基点", "event_store")

    async def fake_provider(ctx: SourceContext) -> list[CaseCandidate]:  # type: ignore[type-arg]
        return [store_c, other_c] if ctx.params.get("src") == "store" else [tele_c]

    adapter = IterableAgentAdapter(
        agent_id="x", module_path="x",
        case_sources=(
            SimpleNamespace(provider="store", params={"src": "store"}),
            SimpleNamespace(provider="tele", params={"src": "tele"}),
        ),
    )
    with patch("aistock_agent.iterate.case_sourcers.SOURCE_PROVIDERS", {
        "store": fake_provider, "tele": fake_provider,
    }):
        results = asyncio.run(source_cases(adapter))
    assert len(results) == 2            # store_c + other_c（tele_c 被指纹去重）
    assert [c.meta["source"] for c in results] == ["event_store", "event_store"]
