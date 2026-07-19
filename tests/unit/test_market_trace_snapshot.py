"""市场溯源事实快照测试 — 事实与因果分离、来源归一化、主导现象确定性。

TDD RED -> GREEN: 先写失败测试，锁定"事实而非因果"的契约。
"""

from unittest.mock import AsyncMock

import pytest

from aistock_agent.services.data_client import node_api
from aistock_agent.services.market_trace_snapshot import (
    MarketTraceSnapshotUnavailable,
    build_market_trace_snapshot,
    select_dominant_phenomenon,
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

# 触发 broad_rally 的 a_share 事实（用于主导现象确定性测试）
RALLY_FACTS: dict[str, object] = {
    "indexes": [
        {"ts_code": "000001.SH", "pct_chg": 1.2},
        {"ts_code": "399001.SZ", "pct_chg": 1.5},
        {"ts_code": "399006.SZ", "pct_chg": 1.8},
        {"ts_code": "000300.SH", "pct_chg": 1.0},
        {"ts_code": "000905.SH", "pct_chg": 0.9},
        {"ts_code": "000852.SH", "pct_chg": 1.1},
    ],
    "breadth": {"advance_ratio": 0.75, "total_count": 5000, "decline_count": 1000},
    "limits": {"up_count": 50, "down_count": 10, "broken_count": 5, "highest_board": 3},
    "turnover": {"change_pct": 15.0},
    "sectors": {"top_gainers": [], "top_losers": []},
    "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
}


# ============================================================================
# Step 1 verbatim 测试 — 事实与证据分离
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_keeps_facts_and_evidence_separate(mocker):
    mocker.patch.object(node_api, "get", AsyncMock(return_value=COMPLETE_CLOSE))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        return_value=[GLOBAL_FACT],
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


# ============================================================================
# 不可用场景 — Node 返回 None 或 coverage 不完整时立即失败
# ============================================================================


@pytest.mark.asyncio
async def test_snapshot_raises_when_node_returns_none(mocker):
    """Node 返回 None 时抛出 MarketTraceSnapshotUnavailable。"""
    mocker.patch.object(node_api, "get", AsyncMock(return_value=None))
    mocker.patch(
        "aistock_agent.services.market_trace_snapshot.collect_global_market_facts",
        return_value=[],
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
        return_value=[],
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
        return_value=[],
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
        return_value=[],  # 无境外行情
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
        return_value=[GLOBAL_FACT],
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
# 主导现象确定性 — 相同输入每次选择相同结果
# ============================================================================


def test_dominant_phenomenon_is_deterministic():
    """相同 a_share_facts 输入每次返回相同的 dominant_phenomenon。"""
    result1 = select_dominant_phenomenon(RALLY_FACTS)
    result2 = select_dominant_phenomenon(RALLY_FACTS)
    assert result1 is not None
    assert result2 is not None
    assert result1.kind == result2.kind
    assert result1.score == result2.score
    assert result1.fact_ids == result2.fact_ids


def test_dominant_phenomenon_returns_broad_rally_for_rally_facts():
    """RALLY_FACTS 触发 broad_rally（基础 + 两项加分）。"""
    result = select_dominant_phenomenon(RALLY_FACTS)
    assert result is not None
    assert result.kind == "broad_rally"
    assert result.score >= 2


def test_dominant_phenomenon_returns_none_when_no_rule_qualifies():
    """COMPLETE_CLOSE 的盘面数据平淡，无规则达到两项信号。"""
    result = select_dominant_phenomenon(COMPLETE_CLOSE)
    assert result is None


def test_dominant_phenomenon_returns_none_for_empty_input():
    """空输入返回 None。"""
    assert select_dominant_phenomenon({}) is None
    assert select_dominant_phenomenon({"indexes": []}) is None
