"""CHAT QA Skills 单元测试。"""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal
from aistock_agent.skills.report_lookup import report_lookup


def _goal(intent: str = "report_lookup") -> InsightGoal:
    return InsightGoal(question="今天晨报说了什么", intent=intent)


@pytest.mark.asyncio
async def test_report_lookup_review_hit():
    """review 报告命中缓存 → 正常 Evidence。"""
    fake_artifact = {
        "schema_version": "1.1",
        "markdown": "# 复盘\n今日市场涨跌...",
        "trace_summary": "白酒板块领涨",
        "sectors": ["baijiu"],
    }
    with patch(
        "aistock_agent.skills.report_lookup.get_cached_review",
        new=AsyncMock(return_value=fake_artifact),
    ):
        ev = await report_lookup({"report_type": "review", "date": "2026-07-28"}, _goal())
    assert ev.skill_name == "report_lookup"
    assert ev.degraded is False
    assert len(ev.facts) >= 1
    assert any(s.kind == "db_report" for s in ev.sources)


@pytest.mark.asyncio
async def test_report_lookup_miss_returns_degraded():
    """缓存未命中 → degraded Evidence。"""
    with patch(
        "aistock_agent.skills.report_lookup.get_cached_review",
        new=AsyncMock(return_value=None),
    ):
        ev = await report_lookup({"report_type": "review", "date": "1999-01-01"}, _goal())
    assert ev.degraded is True
    assert "未找到" in (ev.degraded_reason or "") or "miss" in (ev.degraded_reason or "").lower()


from aistock_agent.skills.stock_snapshot import stock_snapshot


@pytest.mark.asyncio
async def test_stock_snapshot_normal():
    fake_quote = "600519 当前价 1800.00 涨跌幅 +2.5%"
    with patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(return_value=fake_quote),
    ):
        ev = await stock_snapshot(
            {"symbol": "600519"},
            InsightGoal(question="茅台现在多少钱", intent="stock_snapshot", symbols=["600519"]),
        )
    assert ev.skill_name == "stock_snapshot"
    assert ev.degraded is False
    assert any("1800" in f or "2.5" in f for f in ev.facts)
    assert ev.symbols == ["600519"]


@pytest.mark.asyncio
async def test_stock_snapshot_tool_exception_degraded():
    """工具内部异常被 @skill 装饰器捕获 → degraded。"""
    with patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=AsyncMock(side_effect=RuntimeError("network timeout")),
    ):
        ev = await stock_snapshot(
            {"symbol": "600519"},
            InsightGoal(question="x", intent="stock_snapshot", symbols=["600519"]),
        )
    assert ev.degraded is True
    assert "stock_snapshot" in (ev.degraded_reason or "")


# ── stock_news ──────────────────────────────────────────────────────────
from aistock_agent.skills.stock_news import stock_news


@pytest.mark.asyncio
async def test_stock_news_normal():
    fake_news = "1. 茅台发布半年报\n2. 茅台召开投资者交流会"
    with patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=AsyncMock(return_value=fake_news),
    ):
        ev = await stock_news(
            {"symbol": "600519", "limit": 10},
            InsightGoal(question="茅台最近新闻", intent="stock_news", symbols=["600519"]),
        )
    assert ev.skill_name == "stock_news"
    assert ev.degraded is False
    assert any("半年报" in f or "交流会" in f for f in ev.facts)


@pytest.mark.asyncio
async def test_stock_news_exception_degraded():
    with patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=AsyncMock(side_effect=RuntimeError("cls api down")),
    ):
        ev = await stock_news(
            {"symbol": "600519"},
            InsightGoal(question="x", intent="stock_news", symbols=["600519"]),
        )
    assert ev.degraded is True
