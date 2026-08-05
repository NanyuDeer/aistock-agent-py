"""市场溯源事实快照测试 — 事实与因果分离、来源归一化、现象发现确定性。

TDD RED -> GREEN: 先写失败测试，锁定"事实而非因果"的契约。
"""

import copy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from aistock_agent.schemas.market_trace import (
    DataReadiness,
    MarketTraceSnapshot,
    PhenomenonDiscoveryResult,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.market_trace_snapshot import (
    MarketTraceSnapshotUnavailable,
    build_market_trace_snapshot,
    build_quick_snapshot,
    normalize_a_share,
)

# ============================================================================
# 测试 fixtures
# ============================================================================

# Node /internal/market/close-snapshot 返回的完整收盘快照。
# 同时充当 /internal/news/latest 的返回值（因 verbatim 测试用 return_value 统一 mock），
# 故包含 items 键供新闻解析使用。
COMPLETE_CLOSE: dict[str, object] = {
    "schema_version": "1.0",
    "status": "complete",
    "trade_date": "20260719",
    "captured_at": "2026-07-19T07:31:00.000Z",
    "indexes": [
        {
            "ts_code": "000001.SH",
            "name": "上证指数",
            "trade_date": "20260719",
            "close": 3200.0,
            "pct_chg": 0.5,
            "amount": 100000.0,
            "source": "tushare:index_daily",
        },
        {
            "ts_code": "399001.SZ",
            "name": "深证成指",
            "trade_date": "20260719",
            "close": 10500.0,
            "pct_chg": 0.4,
            "amount": 120000.0,
            "source": "tushare:index_daily",
        },
        {
            "ts_code": "399006.SZ",
            "name": "创业板指",
            "trade_date": "20260719",
            "close": 2100.0,
            "pct_chg": 0.3,
            "amount": 50000.0,
            "source": "tushare:index_daily",
        },
        {
            "ts_code": "000300.SH",
            "name": "沪深300",
            "trade_date": "20260719",
            "close": 3800.0,
            "pct_chg": 0.2,
            "amount": 80000.0,
            "source": "tushare:index_daily",
        },
        {
            "ts_code": "000905.SH",
            "name": "中证500",
            "trade_date": "20260719",
            "close": 5500.0,
            "pct_chg": 0.1,
            "amount": 60000.0,
            "source": "tushare:index_daily",
        },
        {
            "ts_code": "000852.SH",
            "name": "中证1000",
            "trade_date": "20260719",
            "close": 6000.0,
            "pct_chg": 0.05,
            "amount": 70000.0,
            "source": "tushare:index_daily",
        },
    ],
    "breadth": {
        "total_count": 5000,
        "advance_count": 2500,
        "decline_count": 2000,
        "flat_count": 500,
        "advance_ratio": 0.5,
        "source": "tushare:daily",
    },
    "turnover": {
        "amount_yuan": 95_000_000_000,
        "previous_amount_yuan": 90_000_000_000,
        "change_pct": 5.0,
        "source": "tushare:daily",
    },
    "limits": {
        "up_count": 20,
        "down_count": 15,
        "broken_count": 5,
        "highest_board": 3,
    },
    "sectors": {
        "top_gainers": [
            {
                "ts_code": "881101",
                "name": "AI芯片",
                "pct_change": 2.5,
                "net_amount": 1_000_000_000,
                "lead_stock": "寒武纪",
                "company_num": 50,
                "trade_date": "20260719",
            },
        ],
        "top_losers": [
            {
                "ts_code": "881102",
                "name": "房地产",
                "pct_change": -2.0,
                "net_amount": -500_000_000,
                "lead_stock": "万科A",
                "company_num": 80,
                "trade_date": "20260719",
            },
        ],
        "top_inflows": [],
        "top_outflows": [],
    },
    "main_force": {
        "large_and_extra_large_net_yuan": 5_000_000_000,
        "source": "tushare:moneyflow_ths",
    },
    "coverage": {
        "current_daily": {
            "complete": True,
            "reason": "ok",
            "page_count": 5,
            "row_count": 5000,
        },
        "previous_daily": {
            "complete": True,
            "reason": "ok",
            "page_count": 5,
            "row_count": 5000,
        },
    },
    # 同时充当 /internal/news/latest 返回值中的新闻列表
    "items": [
        {
            "title": "央行宣布降准",
            "brief": "中国人民银行决定下调存款准备金率0.5个百分点",
            "url": "https://www.cls.cn/news/1",
            "time": "2020-01-01T00:00:00Z",
        },
    ],
}

GLOBAL_FACT: dict[str, object] = {
    "ticker": "^GSPC",
    "name": "标普500",
    "price": 5500.0,
    "change_pct": 0.36,
    "observed_at": "2026-07-19T07:31:00+00:00",
}

TAVILY_RESULT: dict[str, object] = {
    "results": [
        {
            "title": "美联储维持利率不变",
            "content": "美联储在最新议息会议上决定维持联邦基金利率目标区间不变",
            "url": "https://example.com/fed",
            "published_date": "2020-01-01T00:00:00Z",
        },
    ],
}


def test_snapshot_requires_frozen_phenomenon_discovery() -> None:
    """旧快照不得以缺失 discovery 的形式通过 Pydantic 解析。"""
    payload = MarketTraceSnapshot(
        snapshot_id="trace-test",
        trade_date="2026-07-19",
        captured_at=datetime(2026, 7, 19, tzinfo=UTC),
        a_share={},
        sources={},
        missing_fields=[],
        phenomenon_discovery=PhenomenonDiscoveryResult(
            status="no_phenomenon",
            primary=None,
            concurrent_phenomena=[],
            data_readiness=DataReadiness(
                market_data="complete",
                attribution_inputs="missing",
                causal_evidence="not_ready",
            ),
            diagnostics=[],
        ),
    ).model_dump(mode="json")
    payload.pop("phenomenon_discovery", None)

    with pytest.raises(ValidationError, match="phenomenon_discovery"):
        MarketTraceSnapshot.model_validate(payload)


# ============================================================================
# Step 1 verbatim 测试 — 事实与证据分离
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_keeps_facts_and_evidence_separate(mocker):
    mocker.patch.object(node_api, "get", AsyncMock(return_value=COMPLETE_CLOSE))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[GLOBAL_FACT]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value=TAVILY_RESULT,
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    assert snapshot.trade_date == "2026-07-19"
    assert snapshot.sources["INDEX_000001_SH"].kind == "market_fact"
    assert snapshot.sources["GLOBAL_001"].kind == "market_fact"
    assert snapshot.sources["NEWS_001"].kind == "event_evidence"
    assert not hasattr(snapshot, "cause")


@pytest.mark.asyncio
async def test_snapshot_normalizes_a_share_indexes_without_mutating_node_payload(mocker):
    close_data = copy.deepcopy(COMPLETE_CLOSE)
    indexes = close_data["indexes"]
    assert isinstance(indexes, list)
    indexes.append({"ts_code": "../../invalid", "pct_chg": 9.9})
    original = copy.deepcopy(close_data)
    mocker.patch.object(node_api, "get", AsyncMock(side_effect=[close_data, {"items": []}]))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    assert close_data == original
    normalized_indexes = snapshot.a_share["indexes"]
    assert isinstance(normalized_indexes, dict)
    assert list(normalized_indexes) == [
        "SH000001",
        "SZ399001",
        "SZ399006",
        "SH000300",
        "SH000905",
        "SH000852",
    ]
    shanghai = normalized_indexes["SH000001"]
    assert shanghai["ts_code"] == "000001.SH"
    assert shanghai["change_pct"] == 0.5
    assert shanghai["source_id"] == "INDEX_000001_SH"
    assert "pct_chg" not in shanghai
    assert "../../invalid" not in normalized_indexes
    assert "INDEX_000001_SH" in snapshot.sources
    assert snapshot.phenomenon_discovery.data_readiness.market_data == "complete"


def test_normalize_a_share_keeps_already_normalized_index_change_pct():
    close_data = {
        "indexes": [
            {
                "ts_code": "000001.SH",
                "change_pct": -1.2,
            }
        ]
    }

    normalized = normalize_a_share(close_data)

    indexes = normalized["indexes"]
    assert isinstance(indexes, dict)
    assert indexes["SH000001"]["change_pct"] == -1.2


def test_normalize_a_share_accepts_legacy_tencent_index_code():
    result = normalize_a_share({"indexes": [{"ts_code": "sh000001", "pct_chg": 0.5}]})

    index = result["indexes"]["SH000001"]
    assert index["ts_code"] == "000001.SH"
    assert index["source_id"] == "INDEX_000001_SH"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "missing_value", "source_id", "missing_field"),
    [
        (
            "turnover",
            {
                "amount_yuan": None,
                "previous_amount_yuan": None,
                "change_pct": None,
                "source": "tushare:daily",
            },
            "TURNOVER_ALL",
            "a_share.turnover",
        ),
        (
            "limits",
            {
                "up_count": None,
                "down_count": None,
                "broken_count": None,
                "highest_board": None,
            },
            "LIMITS_ALL",
            "a_share.limits",
        ),
        (
            "main_force",
            {
                "large_and_extra_large_net_yuan": None,
                "source": "tushare:moneyflow_ths",
            },
            "MAIN_FORCE_ALL",
            "a_share.main_force.large_and_extra_large_net_yuan",
        ),
    ],
)
async def test_snapshot_omits_aggregate_source_without_real_numeric_facts(
    mocker,
    field: str,
    missing_value: dict[str, object],
    source_id: str,
    missing_field: str,
) -> None:
    close_data = copy.deepcopy(COMPLETE_CLOSE)
    indexes = close_data["indexes"]
    assert isinstance(indexes, list)
    for index in indexes:
        assert isinstance(index, dict)
        index["pct_chg"] = 1.2
    close_data["breadth"] = {
        "total_count": 5000,
        "advance_count": 3750,
        "decline_count": 1000,
        "flat_count": 250,
        "advance_ratio": 0.75,
        "source": "tushare:daily",
    }
    close_data["turnover"] = {
        "amount_yuan": 110_000_000_000,
        "previous_amount_yuan": 90_000_000_000,
        "change_pct": 15.0,
        "source": "tushare:daily",
    }
    close_data["limits"] = {
        "up_count": 50,
        "down_count": 10,
        "broken_count": 5,
        "highest_board": 3,
    }
    close_data[field] = missing_value
    mocker.patch.object(node_api, "get", AsyncMock(side_effect=[close_data, {"items": []}]))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    assert snapshot.phenomenon_discovery.status == "detected"
    primary = snapshot.phenomenon_discovery.primary
    assert primary is not None
    assert source_id not in snapshot.sources
    assert source_id not in primary.fact_ids
    assert missing_field in snapshot.missing_fields


@pytest.mark.asyncio
async def test_snapshot_omits_breadth_source_without_real_numeric_facts(mocker) -> None:
    close_data = copy.deepcopy(COMPLETE_CLOSE)
    indexes = close_data["indexes"]
    assert isinstance(indexes, list)
    for index in indexes:
        assert isinstance(index, dict)
        index["pct_chg"] = 1.5
    close_data["breadth"] = {
        "advance_ratio": None,
        "decline_ratio": None,
        "source": "tushare:daily",
    }
    close_data["turnover"] = {
        "amount_yuan": 110_000_000_000,
        "previous_amount_yuan": 90_000_000_000,
        "change_pct": 15.0,
        "source": "tushare:daily",
    }
    mocker.patch.object(node_api, "get", AsyncMock(side_effect=[close_data, {"items": []}]))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    assert snapshot.phenomenon_discovery.status == "detected"
    detected = [
        snapshot.phenomenon_discovery.primary,
        *snapshot.phenomenon_discovery.concurrent_phenomena,
    ]
    fact_ids = {fact_id for phenomenon in detected if phenomenon for fact_id in phenomenon.fact_ids}
    assert "BREADTH_ALL" not in snapshot.sources
    assert "BREADTH_ALL" not in fact_ids
    assert "a_share.breadth" in snapshot.missing_fields


@pytest.mark.asyncio
async def test_snapshot_omits_sectors_source_when_all_rankings_are_empty(mocker) -> None:
    close_data = copy.deepcopy(COMPLETE_CLOSE)
    indexes = close_data["indexes"]
    assert isinstance(indexes, list)
    for index in indexes:
        assert isinstance(index, dict)
        index["pct_chg"] = 1.5
    close_data["sectors"] = {
        "top_gainers": [],
        "top_losers": [],
        "top_inflows": [],
        "top_outflows": [],
    }
    close_data["turnover"] = {
        "amount_yuan": 110_000_000_000,
        "previous_amount_yuan": 90_000_000_000,
        "change_pct": 15.0,
        "source": "tushare:daily",
    }
    mocker.patch.object(node_api, "get", AsyncMock(side_effect=[close_data, {"items": []}]))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    snapshot = await build_market_trace_snapshot("2026-07-19")

    assert snapshot.phenomenon_discovery.status == "detected"
    detected = [
        snapshot.phenomenon_discovery.primary,
        *snapshot.phenomenon_discovery.concurrent_phenomena,
    ]
    fact_ids = {fact_id for phenomenon in detected if phenomenon for fact_id in phenomenon.fact_ids}
    assert "SECTORS_ALL" not in snapshot.sources
    assert "SECTORS_ALL" not in fact_ids
    assert "a_share.sectors" in snapshot.missing_fields


# ============================================================================
# 不可用场景 — Node 返回 None 或 coverage 不完整时立即失败
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_raises_when_node_returns_none(mocker):
    """Node 返回 None 时抛出 MarketTraceSnapshotUnavailable。"""
    mocker.patch.object(node_api, "get", AsyncMock(return_value=None))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    with pytest.raises(MarketTraceSnapshotUnavailable):
        await build_market_trace_snapshot("2026-07-19")


@pytest.mark.asyncio
async def test_snapshot_raises_when_status_not_complete(mocker):
    """Node 返回 status 非 complete 时抛出异常。"""
    incomplete = {**COMPLETE_CLOSE, "status": "partial"}
    mocker.patch.object(node_api, "get", AsyncMock(return_value=incomplete))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    with pytest.raises(MarketTraceSnapshotUnavailable):
        await build_market_trace_snapshot("2026-07-19")


@pytest.mark.asyncio
async def test_snapshot_raises_when_coverage_incomplete(mocker):
    """coverage.current_daily.complete 非 True 时抛出异常。"""
    incomplete = {
        **COMPLETE_CLOSE,
        "coverage": {
            "current_daily": {"complete": False, "reason": "empty"},
            "previous_daily": {"complete": True, "reason": "ok"},
        },
    }
    mocker.patch.object(node_api, "get", AsyncMock(return_value=incomplete))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    with pytest.raises(MarketTraceSnapshotUnavailable):
        await build_market_trace_snapshot("2026-07-19")


# ============================================================================
# 缺失外部来源 — 进入 missing_fields，不阻塞 A 股事实
# ============================================================================


@pytest.mark.asyncio
async def test_missing_external_sources_go_to_missing_fields(mocker):
    """境外行情、Tavily 结果缺失时进入 missing_fields，不阻塞快照构建。"""
    mocker.patch.object(node_api, "get", AsyncMock(return_value=COMPLETE_CLOSE))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),  # 无境外行情
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},  # 无 Tavily 结果
    )
    snapshot = await build_market_trace_snapshot("2026-07-19")
    assert "global_markets" in snapshot.missing_fields
    assert "tavily_search_1" in snapshot.missing_fields
    assert "tavily_search_2" in snapshot.missing_fields
    # A 股事实仍完整存在
    assert "INDEX_000001_SH" in snapshot.sources
    assert "BREADTH_ALL" in snapshot.sources


# ============================================================================
# 时间过滤 — 发生时间晚于 captured_at 的新闻/检索不得进入快照
# ============================================================================


@pytest.mark.asyncio
async def test_future_news_excluded_from_snapshot(mocker):
    """发生在 captured_at 之后的新闻不得进入快照。"""
    close_with_future_news = {
        **COMPLETE_CLOSE,
        "items": [
            {
                "title": "未来事件",
                "brief": "发生在 captured_at 之后",
                "url": "https://example.com/future",
                "time": "2099-12-31T23:59:59Z",
            },
            {
                "title": "正常事件",
                "brief": "发生在 captured_at 之前",
                "url": "https://example.com/normal",
                "time": "2020-01-01T00:00:00Z",
            },
        ],
    }
    mocker.patch.object(node_api, "get", AsyncMock(return_value=close_with_future_news))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[GLOBAL_FACT]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value=TAVILY_RESULT,
    )
    snapshot = await build_market_trace_snapshot("2026-07-19")
    # 未来事件被过滤，正常事件获得 NEWS_001
    assert snapshot.sources["NEWS_001"].title == "正常事件"
    assert "NEWS_002" not in snapshot.sources


# ============================================================================
# Task 5 review 修复 — 同时校验 previous_daily.complete + trade_date/report_date 一致性
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_raises_when_previous_daily_incomplete(mocker):
    """coverage.previous_daily.complete 非 True 时抛出异常。

    Node 当日 facts 必须与 previous_daily 共同完整；只校验 current_daily 会放过
    Node 把"今日已收盘"伪装成 complete、但 previous_daily 仍滞后的场景。
    """
    incomplete = {
        **COMPLETE_CLOSE,
        "coverage": {
            "current_daily": {"complete": True, "reason": "ok"},
            "previous_daily": {"complete": False, "reason": "empty"},
        },
    }
    mocker.patch.object(node_api, "get", AsyncMock(return_value=incomplete))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    with pytest.raises(MarketTraceSnapshotUnavailable):
        await build_market_trace_snapshot("2026-07-19")


@pytest.mark.asyncio
async def test_snapshot_raises_when_trade_date_mismatches_report_date(mocker):
    """Node trade_date 与 report_date 不一致时降级，不把旧事实写入新日期快照。

    场景：周末/节假日调用时 Node 没有当日数据，trade_date 仍是上一交易日。
    若不严格校验，会把上一交易日的 facts 伪装成"今日"快照写入。
    """
    # Node 返回的 trade_date 是 20260717（上一交易日），但 report_date 是 2026-07-19
    stale = {**COMPLETE_CLOSE, "trade_date": "20260717"}
    mocker.patch.object(node_api, "get", AsyncMock(return_value=stale))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    with pytest.raises(MarketTraceSnapshotUnavailable):
        await build_market_trace_snapshot("2026-07-19")


@pytest.mark.asyncio
@pytest.mark.parametrize("trade_date", ["20260719", "2026-07-19"])
async def test_snapshot_normalizes_trade_date_for_report_date_check(mocker, trade_date):
    """trade_date 既支持 YYYYMMDD 也支持 YYYY-MM-DD，规范化后与 report_date 比较。"""
    # Node trade_date 支持 YYYYMMDD 和 YYYY-MM-DD；report_date 固定为 YYYY-MM-DD。
    normalized = {
        **COMPLETE_CLOSE,
        "trade_date": trade_date,
    }
    mocker.patch.object(node_api, "get", AsyncMock(return_value=normalized))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    snapshot = await build_market_trace_snapshot("2026-07-19")
    assert snapshot.trade_date == "2026-07-19"
    expected_occurred_at = datetime(2026, 7, 19, tzinfo=UTC)
    for source_id in (
        "INDEX_000001_SH",
        "BREADTH_ALL",
        "TURNOVER_ALL",
        "LIMITS_ALL",
        "MAIN_FORCE_ALL",
        "SECTORS_ALL",
    ):
        assert snapshot.sources[source_id].occurred_at == expected_occurred_at


@pytest.mark.asyncio
async def test_snapshot_date_mismatch_blocks_external_calls(mocker):
    """Node trade_date 与 report_date 不一致时，不调用任何外部数据源。

    场景：周末/节假日调用时 Node 没有当日数据，trade_date 仍是上一交易日。
    修复前：trade_date 校验在 collect_global_market_facts、node 新闻接口和
    Tavily 调用之后才执行，浪费外部 API 配额；修复后：日期不一致时立即抛
    MarketTraceSnapshotUnavailable，不调用任何外部数据源。

    注意：node_api.get("/internal/market/close-snapshot") 仍会被调用一次
    （因为 trade_date 来自 close-snapshot 响应），但不应被调用第二次
    （用于 /internal/news/latest）。

    强断言：旧实现用 side_effect 抛 AssertionError 的 mock 检查 yfinance/Tavily
    未调用，但生产代码 try/except Exception 会吞掉 AssertionError，存在假阳性。
    这里保留 mock 引用，在异常断言后用 assert_not_called() 明确断言。
    """
    stale = {**COMPLETE_CLOSE, "trade_date": "20260717"}
    # 用 side_effect 记录调用顺序；如果日期校验前置，第二次 node_api.get 不应被调用
    node_get_calls: list[str] = []

    async def _node_get_side_effect(path: str, **_kwargs):
        node_get_calls.append(path)
        if path == "/internal/market/close-snapshot":
            return stale
        return {"items": []}

    mocker.patch.object(node_api, "get", side_effect=_node_get_side_effect)

    # 保留 global market 和 Tavily mock 的引用，用于 assert_not_called() 强断言。
    # 不再使用 side_effect 抛 AssertionError，因为生产代码 try/except Exception
    # 会吞掉该异常，导致假阳性。
    global_market_mock = mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    tavily_search_mock = mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    with pytest.raises(MarketTraceSnapshotUnavailable):
        await build_market_trace_snapshot("2026-07-19")

    # 日期不一致时不得调用任何外部数据源。
    global_market_mock.assert_not_called()
    tavily_search_mock.assert_not_called()
    # 只应调用过 close-snapshot，不应调用 news/latest
    assert node_get_calls == ["/internal/market/close-snapshot"]


# ============================================================================
# Task 5 review 修复 — snapshot_id 必须支持同日失败后的安全重试
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_id_includes_captured_at_for_safe_retry(mocker):
    """同日失败后的重试必须产生不同 snapshot_id，避免 facts.json FileExistsError 永久阻断。

    场景：首次 LLM/校验失败后，归档目录已有 trace-{trade_date}-facts.json。
    若 snapshot_id 仅基于 trade_date，重试时 archive_market_trace_snapshot 会抛
    FileExistsError，永久阻断后续重试。修复方案：snapshot_id 包含 captured_at 时间戳，
    不同 captured_at 产生不同 snapshot_id，facts 文件仍不可覆盖但允许同日新建。
    """
    fixed_now_1 = datetime(2026, 7, 19, 7, 31, 0, tzinfo=UTC)
    fixed_now_2 = datetime(2026, 7, 19, 7, 35, 30, tzinfo=UTC)

    mocker.patch.object(node_api, "get", AsyncMock(return_value=COMPLETE_CLOSE))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    real_datetime = datetime
    time_queue = [fixed_now_1, fixed_now_2]

    class _FakeDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if time_queue:
                return time_queue.pop(0)
            return real_datetime.now(tz)

    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.datetime",
        _FakeDateTime,
    )

    snapshot_1 = await build_market_trace_snapshot("2026-07-19")
    snapshot_2 = await build_market_trace_snapshot("2026-07-19")

    # 同一天 trade_date 必须相同
    assert snapshot_1.trade_date == snapshot_2.trade_date == "2026-07-19"
    # 不同 captured_at 必须产生不同 snapshot_id
    assert snapshot_1.captured_at != snapshot_2.captured_at
    assert snapshot_1.snapshot_id != snapshot_2.snapshot_id
    # snapshot_id 仍以 trace-{trade_date_yyyymmdd}- 开头，便于按日期检索
    assert snapshot_1.snapshot_id.startswith("trace-20260719-")
    assert snapshot_2.snapshot_id.startswith("trace-20260719-")


# ============================================================================
# Quick Snapshot 测试（15:30 腾讯实时行情）
# ============================================================================


@pytest.mark.asyncio
async def test_build_quick_snapshot_success(mocker):
    """quick snapshot 成功构建：Node 返回 quick 数据 + 外部来源正常。"""
    from aistock_agent.services.market_trace_snapshot import (
        build_quick_snapshot,
    )

    quick_data: dict[str, object] = {
        "status": "complete",
        "trade_date": "20260730",
        "indexes": [
            {
                "ts_code": "000001.SH",
                "name": "上证指数",
                "close": 3200,
                "pct_chg": 1.2,
                "amount": 3000000000,
            }
        ],
        "breadth": {
            "total_count": 4000,
            "advance_count": 2000,
            "decline_count": 1500,
            "flat_count": 500,
        },
        "coverage": {"current_daily": {"complete": False}},
    }

    mocker.patch.object(node_api, "get_quick_snapshot", AsyncMock(return_value=quick_data))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    snapshot = await build_quick_snapshot("2026-07-30")
    assert snapshot.trade_date == "2026-07-30"
    assert snapshot.snapshot_id.startswith("trace-quick-")
    assert snapshot.phenomenon_discovery is not None


@pytest.mark.asyncio
async def test_build_quick_snapshot_raises_when_node_returns_none(mocker):
    """Node quick-snapshot 返回 None 时抛出 MarketTraceSnapshotUnavailable。"""
    from aistock_agent.services.market_trace_snapshot import (
        MarketTraceSnapshotUnavailable,
        build_quick_snapshot,
    )

    mocker.patch.object(node_api, "get_quick_snapshot", AsyncMock(return_value=None))

    with pytest.raises(MarketTraceSnapshotUnavailable, match="returned None"):
        await build_quick_snapshot("2026-07-30")


@pytest.mark.asyncio
async def test_build_quick_snapshot_raises_on_trade_date_mismatch(mocker):
    """trade_date 不匹配时抛出异常。"""
    from aistock_agent.services.market_trace_snapshot import (
        MarketTraceSnapshotUnavailable,
        build_quick_snapshot,
    )

    mocker.patch.object(
        node_api,
        "get_quick_snapshot",
        AsyncMock(
            return_value={
                "status": "complete",
                "trade_date": "20260729",
                "indexes": [],
                "breadth": {},
                "coverage": {},
            }
        ),
    )

    with pytest.raises(MarketTraceSnapshotUnavailable, match="trade_date"):
        await build_quick_snapshot("2026-07-30")


# ============================================================================
# Task 2 — quick availability and source-collection diagnostics
# ============================================================================


def _quick_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": "complete",
        "trade_date": "20260730",
        "indexes": [
            {
                "ts_code": "000001.SH",
                "name": "上证指数",
                "close": 3200.0,
                "pct_chg": 1.2,
                "amount": 3_000_000_000,
            }
        ],
        "breadth": {
            "total_count": 10,
            "advance_count": 8,
            "decline_count": 2,
            "flat_count": 0,
            "advance_ratio": 0.8,
        },
        "quick_data_availability": {
            "breadth": {"state": "available"},
            "turnover": {"state": "unavailable", "reason": "prior_day_amount_unavailable"},
            "limits": {
                "state": "partial",
                "available_fields": ["up_count", "down_count"],
                "approximate": True,
            },
            "sectors": {"state": "unavailable", "reason": "provider_empty"},
            "main_force": {"state": "unavailable", "reason": "moneyflow_ths_unavailable"},
        },
        "coverage": {"current_daily": {"complete": False}},
    }
    payload.update(overrides)
    return payload


def test_snapshot_diagnostics_defaults_preserve_historical_report_parsing() -> None:
    snapshot = MarketTraceSnapshot(
        snapshot_id="trace-legacy",
        trade_date="2026-07-19",
        captured_at=datetime(2026, 7, 19, tzinfo=UTC),
        a_share={},
        sources={},
        missing_fields=[],
        phenomenon_discovery=PhenomenonDiscoveryResult(
            status="no_phenomenon",
            primary=None,
            concurrent_phenomena=[],
            data_readiness=DataReadiness(
                market_data="complete",
                attribution_inputs="missing",
                causal_evidence="not_ready",
            ),
            diagnostics=[],
        ),
    )

    assert snapshot.data_availability == {}
    assert snapshot.collection_status == {}


@pytest.mark.asyncio
async def test_build_quick_snapshot_uses_canonical_breadth_ratio(mocker) -> None:
    mocker.patch.object(node_api, "get_quick_snapshot", AsyncMock(return_value=_quick_payload()))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    mocker.patch.object(node_api, "get", AsyncMock(return_value={"items": []}))

    snapshot = await build_quick_snapshot("2026-07-30")

    assert "BREADTH_ALL" in snapshot.sources
    assert "advance_ratio=0.8" in snapshot.sources["BREADTH_ALL"].content
    assert snapshot.data_availability["breadth"].state == "available"


@pytest.mark.asyncio
async def test_build_quick_snapshot_omits_unavailable_zero_placeholder_facts(mocker) -> None:
    quick_data = _quick_payload(
        turnover={"amount_yuan": 0, "previous_amount_yuan": 0, "change_pct": 0},
        limits={"up_count": 0, "down_count": 0, "broken_count": 0, "highest_board": 0},
        main_force={"large_and_extra_large_net_yuan": 0},
        sectors={"top_gainers": [], "top_losers": [], "top_inflows": [], "top_outflows": []},
    )
    mocker.patch.object(node_api, "get_quick_snapshot", AsyncMock(return_value=quick_data))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    mocker.patch.object(node_api, "get", AsyncMock(return_value={"items": []}))

    snapshot = await build_quick_snapshot("2026-07-30")

    assert "TURNOVER_ALL" not in snapshot.sources
    assert "MAIN_FORCE_ALL" not in snapshot.sources
    assert "SECTORS_ALL" not in snapshot.sources
    assert "LIMITS_ALL" in snapshot.sources
    assert "approximate=true" in snapshot.sources["LIMITS_ALL"].content
    assert snapshot.data_availability["turnover"].state == "unavailable"
    assert snapshot.data_availability["limits"].state == "partial"


@pytest.mark.asyncio
async def test_snapshot_persists_empty_and_invalid_source_collection_statuses(mocker) -> None:
    mocker.patch.object(node_api, "get_quick_snapshot", AsyncMock(return_value=_quick_payload()))
    mocker.patch.object(node_api, "get", AsyncMock(return_value={"items": []}))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        side_effect=[
            {"results": [{"title": "无 URL", "published_date": "2020-01-01T00:00:00Z"}]},
            {"results": []},
        ],
    )

    snapshot = await build_quick_snapshot("2026-07-30")

    assert snapshot.collection_status["global_markets"].model_dump() == {
        "state": "empty",
        "provider": "tencent:quote",
        "item_count": 0,
        "reason": "provider_returned_no_items",
    }
    assert snapshot.collection_status["cls_news"].state == "empty"
    assert snapshot.collection_status["tavily_domestic_policy"].model_dump() == {
        "state": "invalid_for_causality",
        "provider": "tavily",
        "item_count": 1,
        "reason": "items_missing_url",
    }
    assert snapshot.collection_status["tavily_global_risk"].state == "empty"
    assert snapshot.sources["SEARCH_001"].url is None


@pytest.mark.asyncio
async def test_quick_partial_limits_preserve_any_declared_numeric_field_and_safe_reason(
    mocker,
) -> None:
    quick_data = _quick_payload(
        limits={"broken_count": 0},
        quick_data_availability={
            "breadth": {"state": "available"},
            "turnover": {
                "state": "unavailable",
                "reason": "Authorization: Bearer secret-token",
            },
            "limits": {
                "state": "partial",
                "available_fields": ["broken_count"],
                "approximate": True,
            },
            "sectors": {"state": "unavailable", "reason": "provider_empty"},
            "main_force": {"state": "unavailable", "reason": "moneyflow_ths_unavailable"},
        },
    )
    mocker.patch.object(node_api, "get_quick_snapshot", AsyncMock(return_value=quick_data))
    mocker.patch.object(node_api, "get", AsyncMock(return_value={"items": []}))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    snapshot = await build_quick_snapshot("2026-07-30")

    assert snapshot.sources["LIMITS_ALL"].content == "broken_count=0, approximate=true"
    assert snapshot.data_availability["limits"].state == "partial"
    assert snapshot.data_availability["turnover"].reason == "provider_reported_unavailable"


@pytest.mark.asyncio
async def test_quick_available_zero_market_observations_remain_facts(mocker) -> None:
    quick_data = _quick_payload(
        turnover={"amount_yuan": 0, "previous_amount_yuan": 0, "change_pct": 0},
        limits={"up_count": 0, "down_count": 0, "broken_count": 0, "highest_board": 0},
        main_force={"large_and_extra_large_net_yuan": 0},
        quick_data_availability={
            "breadth": {"state": "available"},
            "turnover": {"state": "available"},
            "limits": {"state": "available"},
            "sectors": {"state": "unavailable", "reason": "provider_empty"},
            "main_force": {"state": "available"},
        },
    )
    mocker.patch.object(node_api, "get_quick_snapshot", AsyncMock(return_value=quick_data))
    mocker.patch.object(node_api, "get", AsyncMock(return_value={"items": []}))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )

    snapshot = await build_quick_snapshot("2026-07-30")

    assert {"TURNOVER_ALL", "LIMITS_ALL", "MAIN_FORCE_ALL"}.issubset(snapshot.sources)
    assert "a_share.turnover" not in snapshot.missing_fields
    assert "a_share.limits" not in snapshot.missing_fields
    assert "a_share.main_force.large_and_extra_large_net_yuan" not in snapshot.missing_fields


# ============================================================================
# Task 3 — morning_forecast 注入 snapshot（成功注入 + 失败降级）
# ============================================================================


@pytest.mark.asyncio
async def test_build_market_trace_snapshot_with_morning_forecast(mocker):
    """snapshot 成功注入 morning_forecast。"""
    from aistock_agent.schemas.market_trace import MorningForecast
    from aistock_agent.services import market_trace_snapshot as mts

    mock_forecast = MorningForecast(
        report_date="2026-07-19",
        summary="A股震荡上行",
        major_events=[],
        sectors=[],
        risks=[],
        source_report_id="rpt_001",
    )

    # 复用现有 COMPLETE_CLOSE 作为 close-snapshot + news/latest 共用 mock
    mocker.patch.object(node_api, "get", AsyncMock(return_value=COMPLETE_CLOSE))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=mock_forecast),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-07-19")
    assert snapshot.morning_forecast is not None
    assert snapshot.morning_forecast.summary == "A股震荡上行"
    assert snapshot.morning_forecast.source_report_id == "rpt_001"
    # 成功时不应写入 missing_fields
    assert "morning_forecast" not in snapshot.missing_fields


@pytest.mark.asyncio
async def test_build_market_trace_snapshot_morning_failure_degraded(mocker):
    """morning 提取失败时 snapshot.morning_forecast=None，写入 missing_fields。"""
    from aistock_agent.services import market_trace_snapshot as mts

    mocker.patch.object(node_api, "get", AsyncMock(return_value=COMPLETE_CLOSE))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    # extract_morning_forecast 返回 None（报告缺失/提取失败）
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=None),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-07-19")
    assert snapshot.morning_forecast is None
    assert "morning_forecast" in snapshot.missing_fields


# ============================================================================
# Task 5 — 财联社电报数据源 + 降级到 latest
# ============================================================================


@pytest.mark.asyncio
async def test_build_market_trace_snapshot_with_telegraph(mocker):
    """电报接口成功时，snapshot.sources 含 NEWS_* 来自电报。"""
    from aistock_agent.services import market_trace_snapshot as mts

    telegraph_data = {
        "date": "2026-07-19",
        "items": [
            {
                "id": 1,
                "title": "央行降准",
                "content": "内容1",
                "time": "2026-07-19 10:00:00",
                "timestamp": 1752892800,
            },
            {
                "id": 2,
                "title": "美股收涨",
                "content": "内容2",
                "time": "2026-07-19 11:00:00",
                "timestamp": 1752896400,
            },
        ],
        "total": 2,
        "degraded": False,
    }

    async def fake_get(path: str, **_kwargs):
        if "/internal/news/telegraph" in path:
            return telegraph_data
        if "/internal/market/close-snapshot" in path:
            return COMPLETE_CLOSE
        return None

    mocker.patch.object(node_api, "get", AsyncMock(side_effect=fake_get))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    # 避免 morning_forecast 干扰 news 测试
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=None),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-07-19")
    news_sources = [
        s for s in snapshot.sources.values() if s.source_id.startswith("NEWS_")
    ]
    assert len(news_sources) == 2
    assert news_sources[0].title == "央行降准"


@pytest.mark.asyncio
async def test_build_market_trace_snapshot_telegraph_fallback_to_latest(mocker):
    """电报接口失败时降级到 /internal/news/latest。"""
    from aistock_agent.services import market_trace_snapshot as mts

    latest_data = {
        "stockName": "",
        "keyword": "",
        "total": 1,
        "items": [
            {
                "id": 1,
                "title": "最新快讯",
                "content": "内容",
                "time": "2026-07-19 14:00:00",
                "link": "",
            }
        ],
    }

    call_log: list[str] = []

    async def fake_get(path: str, **_kwargs):
        call_log.append(path)
        if "/internal/news/telegraph" in path:
            raise RuntimeError("电报接口不可用")
        if "/internal/news/latest" in path:
            return latest_data
        if "/internal/market/close-snapshot" in path:
            return COMPLETE_CLOSE
        return None

    mocker.patch.object(node_api, "get", AsyncMock(side_effect=fake_get))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        new=AsyncMock(return_value=[]),
    )
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.TavilyService.search",
        return_value={"results": []},
    )
    # 避免 morning_forecast 干扰 news 测试
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.extract_morning_forecast",
        AsyncMock(return_value=None),
    )

    snapshot = await mts.build_market_trace_snapshot("2026-07-19")
    # 验证调用了 telegraph 失败后回退 latest
    assert any("telegraph" in p for p in call_log)
    assert any("latest" in p for p in call_log)
    news_sources = [
        s for s in snapshot.sources.values() if s.source_id.startswith("NEWS_")
    ]
    assert len(news_sources) >= 1
