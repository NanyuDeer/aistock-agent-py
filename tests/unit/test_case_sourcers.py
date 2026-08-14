"""case_sourcers 注册表清单封闭 + provider 候选构造（二期 case-sourcing）。"""

import asyncio
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
