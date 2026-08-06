"""CHAT QA Skills 单元测试。"""
from datetime import UTC, datetime
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


from aistock_agent.skills.stock_snapshot import stock_snapshot  # noqa: E402


@pytest.mark.asyncio
async def test_stock_snapshot_normal():
    fake_quote = "600519 当前价 1800.00 涨跌幅 +2.5%"
    # get_quote 是工具对象，产品代码经 .ainvoke 调用 → 配置子 mock 的 return_value，
    # 而非 AsyncMock(return_value=...)（那只会作用于 AsyncMock 自身，.ainvoke 拿不到）；
    # 同时隔离 node_api.get 与交易时段判断，避免测试依赖真实网络/时钟。
    mock_quote = AsyncMock()
    mock_quote.ainvoke.return_value = fake_quote
    with patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=mock_quote,
    ), patch(
        "aistock_agent.skills.stock_snapshot.node_api.get",
        new=AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.skills.stock_snapshot.trading_session_status",
        return_value=("trading", "交易时段"),
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
    mock_quote = AsyncMock()
    mock_quote.ainvoke.side_effect = RuntimeError("network timeout")
    with patch(
        "aistock_agent.skills.stock_snapshot.get_quote",
        new=mock_quote,
    ):
        ev = await stock_snapshot(
            {"symbol": "600519"},
            InsightGoal(question="x", intent="stock_snapshot", symbols=["600519"]),
        )
    assert ev.degraded is True
    assert "stock_snapshot" in (ev.degraded_reason or "")


# ── stock_news ──────────────────────────────────────────────────────────
from aistock_agent.skills.stock_news import stock_news  # noqa: E402


@pytest.mark.asyncio
async def test_stock_news_normal():
    fake_news = "1. 茅台发布半年报\n2. 茅台召开投资者交流会"
    mock_news = AsyncMock()
    mock_news.ainvoke.return_value = fake_news
    with patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=mock_news,
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
    mock_news = AsyncMock()
    mock_news.ainvoke.side_effect = RuntimeError("cls api down")
    with patch(
        "aistock_agent.skills.stock_news.search_cls_news",
        new=mock_news,
    ):
        ev = await stock_news(
            {"symbol": "600519"},
            InsightGoal(question="x", intent="stock_news", symbols=["600519"]),
        )
    assert ev.degraded is True


# ── trace_lookup ─────────────────────────────────────────────────────────
from aistock_agent.skills.trace_lookup import trace_lookup  # noqa: E402


@pytest.mark.asyncio
async def test_trace_lookup_normal():
    """trace_lookup 复用 resolve_trace_evidence，skill_name 和 kind 不变。"""

    from aistock_agent.schemas.chat_contract import ChatSource

    fake_ev = Evidence(
        facts=["交易日: 2026-07-28"],
        sources=[
            ChatSource(
                source_id="trace:test",
                kind="trace",
                title="市场溯源 2026-07-28",
                snippet="交易日: 2026-07-28",
                captured_at=datetime.now(UTC),
            )
        ],
        as_of=datetime.now(UTC),
        degraded=False,
        skill_name="trace_lookup",
    )
    with patch(
        "aistock_agent.skills.trace_lookup.resolve_trace_evidence",
        new=AsyncMock(return_value=fake_ev),
    ):
        ev = await trace_lookup(
            {"date": "2026-07-28"},
            InsightGoal(question="今天为什么涨", intent="trace_lookup"),
        )
    assert ev.skill_name == "trace_lookup"
    assert ev.degraded is False
    assert any(s.kind == "trace" for s in ev.sources)


@pytest.mark.asyncio
async def test_trace_lookup_miss_degraded():
    """resolve_trace_evidence 返回 degraded → trace_lookup 也是 degraded。"""

    fake_ev = Evidence(
        facts=[],
        sources=[],
        as_of=datetime.now(UTC),
        degraded=True,
        degraded_reason="no valid trace evidence for 1999-01-01",
        skill_name="trace_lookup",
    )
    with patch(
        "aistock_agent.skills.trace_lookup.resolve_trace_evidence",
        new=AsyncMock(return_value=fake_ev),
    ):
        ev = await trace_lookup(
            {"date": "1999-01-01"},
            InsightGoal(question="x", intent="trace_lookup"),
        )
    assert ev.degraded is True
    assert "no valid trace" in (ev.degraded_reason or "").lower()


@pytest.mark.asyncio
async def test_trace_lookup_does_not_import_load_validated_trace():
    """trace_lookup 不再直接导入 load_validated_trace。"""
    import aistock_agent.skills.trace_lookup as tl

    assert (
        not hasattr(tl, "load_validated_trace")
    ), "trace_lookup 不应再直接导入 load_validated_trace"


# ── industry_relation ──────────────────────────────────────────────────────
from aistock_agent.skills.industry_relation import industry_relation  # noqa: E402


@pytest.mark.asyncio
async def test_industry_relation_normal():
    fake_result = "白酒 → 上下游: 食品饮料、包装；龙头: 贵州茅台、五粮液"
    mock_match = AsyncMock()
    mock_match.ainvoke.return_value = fake_result
    with patch(
        "aistock_agent.skills.industry_relation.match_industry_by_keywords",
        new=mock_match,
    ):
        ev = await industry_relation(
            {"keywords": ["白酒"], "tag_codes": []},
            InsightGoal(
                question="白酒板块上下游",
                intent="industry_relation",
                tag_codes=["baijiu"],
            ),
        )
    assert ev.skill_name == "industry_relation"
    assert ev.degraded is False
    assert any("白酒" in f or "茅台" in f for f in ev.facts)


@pytest.mark.asyncio
async def test_industry_relation_exception_degraded():
    mock_match = AsyncMock()
    mock_match.ainvoke.side_effect = RuntimeError("vector db down")
    with patch(
        "aistock_agent.skills.industry_relation.match_industry_by_keywords",
        new=mock_match,
    ):
        ev = await industry_relation(
            {"keywords": ["白酒"]},
            InsightGoal(question="x", intent="industry_relation"),
        )
    assert ev.degraded is True
