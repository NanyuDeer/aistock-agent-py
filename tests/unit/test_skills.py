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


# ── insight_lookup（阶段 2.1：自选股洞察读层） ─────────────────────────────
from aistock_agent.skills.insight_lookup import insight_lookup  # noqa: E402


def _insight_goal() -> InsightGoal:
    return InsightGoal(question="我自选股的异动归因", intent="insight_lookup")


@pytest.mark.asyncio
@patch("aistock_agent.services.data_client.node_api.list_insights")
@patch("aistock_agent.services.data_client.node_api.get_insight")
async def test_insight_lookup_normal(get_insight_mock, list_mock):
    """登录用户自选股洞察 → facts 含主因/摘要，sources kind=insight。

    只读断言：仅调用 list_insights（列表维度），不触发 get_insight/写端点。
    """
    list_mock.return_value = [
        {
            "event_id": "wi_1",
            "symbol": "000001",
            "stock_name": "测试股",
            "event_type": "limit_up_radar",
            "direction": "up",
            "primary_driver": {"label": "涨停主因"},
        },
        {
            "event_id": "wi_2",
            "symbol": "600519",
            "stock_name": "贵州茅台",
            "event_type": "midday_price_move",
            "direction": "up",
            "primary_driver": None,
            "display_report": {"summary": "资金流入推动"},
        },
    ]
    ev = await insight_lookup({"user_id": "o_test", "symbol": "000001"}, _insight_goal())

    assert ev.degraded is False
    assert ev.skill_name == "insight_lookup"
    assert len(ev.facts) == 2
    assert "涨停主因" in ev.facts[0]
    assert "资金流入推动" in ev.facts[1]
    assert ev.sources[0].kind == "insight"
    assert ev.sources[0].source_id == "insight:wi_1"
    assert ev.symbols == ["000001", "600519"]
    # 只读断言：只走列表读取，未触发详情/写方法
    list_mock.assert_awaited_once()
    get_insight_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_insight_lookup_no_login_degraded():
    """未登录（无 user_id）→ degraded，不触网络。"""
    with patch(
        "aistock_agent.services.data_client.node_api.list_insights",
        new=AsyncMock(),
    ) as m:
        ev = await insight_lookup({}, _insight_goal())
    assert ev.degraded is True
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_insight_lookup_no_data_degraded():
    """用户自选股无洞察 → degraded。"""
    with patch(
        "aistock_agent.services.data_client.node_api.list_insights",
        new=AsyncMock(return_value=[]),
    ):
        ev = await insight_lookup({"user_id": "o_test"}, _insight_goal())
    assert ev.degraded is True
    assert "no insight" in (ev.degraded_reason or "")


@pytest.mark.asyncio
async def test_insight_lookup_exception_degraded():
    """Node 调用异常 → degraded 不抛。"""
    with patch(
        "aistock_agent.services.data_client.node_api.list_insights",
        new=AsyncMock(side_effect=RuntimeError("node down")),
    ):
        ev = await insight_lookup({"user_id": "o_test"}, _insight_goal())
    assert ev.degraded is True
    assert "failed" in (ev.degraded_reason or "")


# ── stock_trace_lookup（阶段 2.2：个股异动溯源读层） ─────────────────────────
from aistock_agent.skills.stock_trace_lookup import stock_trace_lookup  # noqa: E402


def _stock_trace_goal() -> InsightGoal:
    return InsightGoal(question="600519 为什么异动", intent="stock_trace_lookup")


@pytest.mark.asyncio
@patch("aistock_agent.services.data_client.node_api.list_stock_traces")
async def test_stock_trace_lookup_normal(list_mock):
    """登录用户异动溯源 → facts 含主因/状态，sources kind=stock_trace。

    只读断言：仅调用 list_stock_traces（不触发写端点）。
    """
    list_mock.return_value = [
        {
            "event_id": "mv:600519:2026-08-25:1787641509681:up",
            "symbol": "600519",
            "stock_name": "贵州茅台",
            "direction": "up",
            "analysis_status": "completed",
            "primary_cause": "业绩超预期",
        },
        {
            "event_id": "mv:600519:2026-08-25:1787641501078:up",
            "symbol": "600519",
            "stock_name": "贵州茅台",
            "direction": "up",
            "analysis_status": "processing",
            "primary_cause": None,
        },
    ]
    ev = await stock_trace_lookup({"user_id": "o_test", "symbol": "600519"}, _stock_trace_goal())

    assert ev.degraded is False
    assert ev.skill_name == "stock_trace_lookup"
    assert len(ev.facts) == 2
    assert "业绩超预期" in ev.facts[0]
    assert "归因分析中" in ev.facts[1]
    assert ev.sources[0].kind == "stock_trace"
    assert ev.sources[0].source_id.startswith("stock_trace:mv:600519")
    # 只读断言：仅触发列表读取
    list_mock.assert_awaited_once()
    assert list_mock.await_args.args[0] == "o_test"


@pytest.mark.asyncio
async def test_stock_trace_lookup_no_login_degraded():
    """未登录 → degraded，不触网络。"""
    with patch(
        "aistock_agent.services.data_client.node_api.list_stock_traces",
        new=AsyncMock(),
    ) as m:
        ev = await stock_trace_lookup({}, _stock_trace_goal())
    assert ev.degraded is True
    m.assert_not_awaited()


@pytest.mark.asyncio
async def test_stock_trace_lookup_no_data_degraded():
    """用户无异动溯源事件 → degraded。"""
    with patch(
        "aistock_agent.services.data_client.node_api.list_stock_traces",
        new=AsyncMock(return_value=[]),
    ):
        ev = await stock_trace_lookup({"user_id": "o_test"}, _stock_trace_goal())
    assert ev.degraded is True
    assert "no stock trace" in (ev.degraded_reason or "")


@pytest.mark.asyncio
async def test_stock_trace_lookup_exception_degraded():
    """Node 调用异常 → degraded 不抛。"""
    with patch(
        "aistock_agent.services.data_client.node_api.list_stock_traces",
        new=AsyncMock(side_effect=RuntimeError("node down")),
    ):
        ev = await stock_trace_lookup({"user_id": "o_test"}, _stock_trace_goal())
    assert ev.degraded is True
    assert "failed" in (ev.degraded_reason or "")
