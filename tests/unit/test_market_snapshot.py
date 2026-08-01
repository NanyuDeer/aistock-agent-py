"""market_snapshot Skill 单元测试。

覆盖场景（按 task brief 1.4 Step 1）：
1. scope=both, snapshot_kind=quick → A 股 quick + 全球行情成功 → 2 条 realtime_quote source
2. full: coverage.current_daily 或 previous_daily 不完整 → A 股 degraded
3. A 股失败、全球成功 → 保留全球数据，degraded_reason 说明 A 股缺失
4. 两来源均不可用 → 完全 degraded
5. 非法 scope/snapshot_kind → @skill 降级且不发请求
"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal

# ── Mock 数据 ──────────────────────────────────────────────────────────────

# Quick snapshot (腾讯行情) 不需要 coverage 校验
QUICK_SNAPSHOT_OK: dict[str, object] = {
    "schema_version": "1.0",
    "status": "complete",
    "snapshot_kind": "quick",
    "trade_date": "20260730",
    "captured_at": "2026-07-30T07:30:00.000Z",
    "indexes": [
        {"ts_code": "000001.SH", "name": "上证指数", "close": 3200.0,
         "pct_chg": 0.5, "amount": 100000.0},
        {"ts_code": "399001.SZ", "name": "深证成指", "close": 10500.0,
         "pct_chg": -0.3, "amount": 120000.0},
    ],
    "breadth": {"total_count": 5000, "advance_count": 2500, "decline_count": 2000,
                "flat_count": 500, "advance_ratio": 0.5},
    "turnover": {"amount_yuan": 95_000_000_000, "previous_amount_yuan": 90_000_000_000,
                 "change_pct": 5.0},
    "limits": {"up_count": 20, "down_count": 15, "broken_count": 5, "highest_board": 3},
    "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
    "sectors": {"top_gainers": [], "top_losers": [], "top_inflows": [], "top_outflows": []},
    "coverage": {"has_limit_pool": True, "has_moneyflow": True},
}

# Full close snapshot (Tushare) 需要 coverage 双重校验
FULL_SNAPSHOT_OK: dict[str, object] = {
    "schema_version": "1.0",
    "status": "complete",
    "snapshot_kind": "full",
    "trade_date": "20260730",
    "captured_at": "2026-07-30T12:31:00.000Z",
    "indexes": [
        {"ts_code": "000001.SH", "name": "上证指数", "close": 3200.0,
         "pct_chg": 0.5, "amount": 100000.0},
        {"ts_code": "399001.SZ", "name": "深证成指", "close": 10500.0,
         "pct_chg": -0.3, "amount": 120000.0},
    ],
    "breadth": {"total_count": 5000, "advance_count": 2500, "decline_count": 2000,
                "flat_count": 500, "advance_ratio": 0.5},
    "turnover": {"amount_yuan": 95_000_000_000, "previous_amount_yuan": 90_000_000_000,
                 "change_pct": 5.0},
    "limits": {"up_count": 20, "down_count": 15, "broken_count": 5, "highest_board": 3},
    "main_force": {"large_and_extra_large_net_yuan": 5_000_000_000},
    "sectors": {"top_gainers": [], "top_losers": [], "top_inflows": [], "top_outflows": []},
    "coverage": {
        "current_daily": {"complete": True, "reason": "ok", "page_count": 5, "row_count": 5000},
        "previous_daily": {"complete": True, "reason": "ok", "page_count": 5, "row_count": 5000},
    },
}

# Full snapshot with coverage.current_daily 不完整
FULL_SNAPSHOT_CURRENT_INCOMPLETE: dict[str, object] = {**FULL_SNAPSHOT_OK, "coverage": {
    "current_daily": {"complete": False, "reason": "incomplete_daily_coverage"},
    "previous_daily": {"complete": True, "reason": "ok"},
}}

# Full snapshot with coverage.previous_daily 不完整
FULL_SNAPSHOT_PREVIOUS_INCOMPLETE: dict[str, object] = {**FULL_SNAPSHOT_OK, "coverage": {
    "current_daily": {"complete": True, "reason": "ok"},
    "previous_daily": {"complete": False, "reason": "stale_data"},
}}

# Global market facts
GLOBAL_FACTS: list[dict[str, object]] = [
    {"ticker": "^GSPC", "name": "标普500", "price": 5500.0, "change_pct": 0.36},
    {"ticker": "^IXIC", "name": "纳斯达克", "price": 17000.0, "change_pct": 0.52},
    {"ticker": "^HSI", "name": "恒生指数", "price": 22000.0, "change_pct": -0.25},
]


def _goal() -> InsightGoal:
    return InsightGoal(
        question="市场概览",
        intent="report_lookup",
    )


# ── Test 1: scope=both, snapshot_kind=quick → 两组均成功 ──────────────────


@pytest.mark.asyncio
async def test_market_snapshot_both_quick():
    """scope=both, snapshot_kind=quick → A 股 quick + 全球成功，2 条 realtime_quote source。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=QUICK_SNAPSHOT_OK)
        mock_to_thread.side_effect = lambda fn, arg: GLOBAL_FACTS  # noqa: ARG005

        ev = await market_snapshot(
            {"scope": "both", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.skill_name == "market_snapshot"
    assert ev.degraded is False
    assert len(ev.sources) == 2
    # A 股 source
    a_share_sources = [s for s in ev.sources if s.source_id.startswith("market:a_share:")]
    assert len(a_share_sources) == 1
    assert a_share_sources[0].kind == "realtime_quote"
    # 全球 source
    global_sources = [s for s in ev.sources if s.source_id.startswith("market:global:")]
    assert len(global_sources) == 1
    assert global_sources[0].kind == "realtime_quote"
    # facts 包含 A 股和全球信息
    fact_text = " ".join(ev.facts)
    assert "上证指数" in fact_text or "深证成指" in fact_text
    assert "标普500" in fact_text
    assert "as_of" in ev.model_fields_set or ev.as_of is not None
    assert ev.raw.get("scope") == "both"
    mock_api.get_quick_snapshot.assert_called_once()


# ── Test 2: full 且 coverage 不完整 → A 股 degraded ──────────────────────


@pytest.mark.asyncio
async def test_market_snapshot_full_current_daily_incomplete():
    """full snapshot: coverage.current_daily.complete 非 True → A 股 degraded。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get = AsyncMock(return_value=FULL_SNAPSHOT_CURRENT_INCOMPLETE)
        mock_to_thread.side_effect = lambda fn, arg: GLOBAL_FACTS  # noqa: ARG005

        ev = await market_snapshot(
            {"scope": "both", "snapshot_kind": "full"},
            _goal(),
        )

    # 全球成功保留，A 股降级
    assert ev.degraded is True
    assert ev.degraded_reason is not None
    assert "A 股" in ev.degraded_reason
    assert len(ev.sources) >= 1
    global_sources = [s for s in ev.sources if s.source_id.startswith("market:global:")]
    assert len(global_sources) == 1
    # Facts 中仍有全球数据
    fact_text = " ".join(ev.facts)
    assert "标普500" in fact_text or "全球" in fact_text
    mock_api.get.assert_called_once_with("/internal/market/close-snapshot")
    # quick-snapshot 不应被调用
    mock_api.get_quick_snapshot.assert_not_called()


@pytest.mark.asyncio
async def test_market_snapshot_full_previous_daily_incomplete():
    """full snapshot: coverage.previous_daily.complete 非 True → A 股 degraded。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get = AsyncMock(return_value=FULL_SNAPSHOT_PREVIOUS_INCOMPLETE)
        mock_to_thread.side_effect = lambda fn, arg: GLOBAL_FACTS  # noqa: ARG005

        ev = await market_snapshot(
            {"scope": "both", "snapshot_kind": "full"},
            _goal(),
        )

    assert ev.degraded is True
    assert ev.degraded_reason is not None
    assert "A 股" in ev.degraded_reason
    assert len(ev.sources) >= 1
    mock_api.get.assert_called_once_with("/internal/market/close-snapshot")


# ── Test 3: A 股失败、全球成功 → 保留全球数据 ───────────────────────────


@pytest.mark.asyncio
async def test_market_snapshot_a_share_fails_global_ok():
    """A 股失败（返回 None）、全球成功 → 保留全球 facts/source，degraded_reason 说明 A 股缺失。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=None)
        mock_api.get_last_close_snapshot = AsyncMock(return_value=None)
        mock_to_thread.side_effect = lambda fn, arg: GLOBAL_FACTS  # noqa: ARG005

        ev = await market_snapshot(
            {"scope": "both", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.degraded is True
    assert ev.degraded_reason is not None
    assert "A 股" in ev.degraded_reason
    # 仍有全球数据
    assert len(ev.sources) == 1
    assert ev.sources[0].source_id.startswith("market:global:")
    fact_text = " ".join(ev.facts)
    assert "标普500" in fact_text


# ── Test 4: 两来源均不可用 → 完全 degraded ──────────────────────────────


@pytest.mark.asyncio
async def test_market_snapshot_both_fail():
    """两来源均不可用时 → 完全 degraded。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=None)
        mock_api.get_last_close_snapshot = AsyncMock(return_value=None)
        mock_to_thread.side_effect = RuntimeError("yfinance unavailable")

        ev = await market_snapshot(
            {"scope": "both", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.degraded is True
    assert len(ev.facts) == 0
    assert len(ev.sources) == 0
    assert ev.degraded_reason is not None
    assert "A 股" in ev.degraded_reason
    assert "全球" in ev.degraded_reason


# ── Test 5: 非法参数 → @skill 降级 ──────────────────────────────────────


@pytest.mark.asyncio
async def test_market_snapshot_invalid_scope():
    """非法 scope → @skill 降级，不发请求。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        ev = await market_snapshot(
            {"scope": "invalid", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.degraded is True
    reason_lower = (ev.degraded_reason or "").lower()
    assert "scope" in reason_lower or "market_snapshot" in reason_lower
    assert len(ev.facts) == 0
    assert len(ev.sources) == 0
    mock_api.get_quick_snapshot.assert_not_called()
    mock_to_thread.assert_not_called()


@pytest.mark.asyncio
async def test_market_snapshot_invalid_snapshot_kind():
    """非法 snapshot_kind → @skill 降级，不发请求。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        ev = await market_snapshot(
            {"scope": "a_share", "snapshot_kind": "invalid"},
            _goal(),
        )

    assert ev.degraded is True
    reason_lower = (ev.degraded_reason or "").lower()
    assert "snapshot_kind" in reason_lower or "market_snapshot" in reason_lower
    mock_api.get_quick_snapshot.assert_not_called()
    mock_api.get.assert_not_called()
    mock_to_thread.assert_not_called()


# ── Test 6: 单一 scope ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_market_snapshot_scope_a_share_only():
    """scope=a_share → 只获取 A 股数据，不获取全球数据。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=QUICK_SNAPSHOT_OK)

        ev = await market_snapshot(
            {"scope": "a_share", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.degraded is False
    assert len(ev.sources) == 1
    assert ev.sources[0].source_id.startswith("market:a_share:")
    mock_to_thread.assert_not_called()


@pytest.mark.asyncio
async def test_market_snapshot_scope_global_only():
    """scope=global → 只获取全球数据，不获取 A 股数据。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.asyncio.to_thread") as mock_to_thread,
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
    ):
        mock_to_thread.side_effect = lambda fn, arg: GLOBAL_FACTS  # noqa: ARG005

        ev = await market_snapshot(
            {"scope": "global", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.degraded is False
    assert len(ev.sources) == 1
    assert ev.sources[0].source_id.startswith("market:global:")
    mock_api.get_quick_snapshot.assert_not_called()
    mock_api.get.assert_not_called()


# ── Test 7: full 正常 → 成功 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_market_snapshot_full_success():
    """full snapshot 正常、coverage 完整 → 成功。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get = AsyncMock(return_value=FULL_SNAPSHOT_OK)
        mock_to_thread.side_effect = lambda fn, arg: GLOBAL_FACTS  # noqa: ARG005

        ev = await market_snapshot(
            {"scope": "both", "snapshot_kind": "full"},
            _goal(),
        )

    assert ev.degraded is False
    assert len(ev.sources) == 2
    mock_api.get.assert_called_once_with("/internal/market/close-snapshot")
    mock_api.get_quick_snapshot.assert_not_called()


# ── Test 8: quick 失败 → last-close 降级回退 ─────────────────────────────

# last-close snapshot（最近已完成交易日，full 格式带 coverage）
LAST_CLOSE_OK: dict[str, object] = {
    "schema_version": "1.0",
    "status": "complete",
    "snapshot_kind": "full",
    "trade_date": "20260731",
    "captured_at": "2026-07-31T12:31:00.000Z",
    "indexes": [
        {"ts_code": "000001.SH", "name": "上证指数", "close": 3804.69,
         "pct_chg": -0.62, "amount": 100000.0},
        {"ts_code": "399001.SZ", "name": "深证成指", "close": 10500.0,
         "pct_chg": -2.73, "amount": 120000.0},
    ],
    "breadth": {"total_count": 5000, "advance_count": 1400, "decline_count": 3600,
                "flat_count": 0, "advance_ratio": 0.28},
    "turnover": {"amount_yuan": 234_000_000_000, "previous_amount_yuan": 200_000_000_000,
                 "change_pct": 17.0},
    "limits": {"up_count": 10, "down_count": 20, "broken_count": 5, "highest_board": 2},
    "main_force": {"large_and_extra_large_net_yuan": -3_000_000_000},
    "sectors": {"top_gainers": [], "top_losers": [], "top_inflows": [], "top_outflows": []},
    "coverage": {
        "current_daily": {"complete": True, "reason": "ok", "page_count": 5, "row_count": 5000},
        "previous_daily": {"complete": True, "reason": "ok", "page_count": 5, "row_count": 5000},
    },
}


@pytest.mark.asyncio
async def test_market_snapshot_quick_fails_falls_back_to_last_close():
    """quick-snapshot 失败（周末 409）→ 自动回退 last-close，degraded=False。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=None)
        mock_api.get_last_close_snapshot = AsyncMock(return_value=LAST_CLOSE_OK)
        mock_to_thread.side_effect = RuntimeError("yfinance unavailable")

        ev = await market_snapshot(
            {"scope": "a_share", "snapshot_kind": "quick"},
            _goal(),
        )

    # 有真实 last-close 数据 → 不算 degraded
    assert ev.degraded is False
    assert len(ev.sources) == 1
    assert ev.sources[0].source_id.startswith("market:a_share:")
    # source 标注最近交易日
    assert "2026-07-31" in ev.sources[0].title or "20260731" in ev.sources[0].source_id
    # facts 含真实指数数据
    fact_text = " ".join(ev.facts)
    assert "上证指数" in fact_text
    assert "3804.69" in fact_text
    # raw 标记降级来源
    assert ev.raw.get("used_last_close") is True
    assert ev.raw.get("trade_date") == "20260731"
    mock_api.get_last_close_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_market_snapshot_quick_fails_last_close_incomplete_coverage():
    """last-close coverage 不完整 → 仍 degraded（A 股）。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    bad_last_close = {**LAST_CLOSE_OK, "coverage": {
        "current_daily": {"complete": False, "reason": "incomplete_daily_coverage"},
        "previous_daily": {"complete": True, "reason": "ok"},
    }}
    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=None)
        mock_api.get_last_close_snapshot = AsyncMock(return_value=bad_last_close)
        mock_to_thread.side_effect = RuntimeError("yfinance unavailable")

        ev = await market_snapshot(
            {"scope": "a_share", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.degraded is True
    assert "A 股" in (ev.degraded_reason or "")
    mock_api.get_last_close_snapshot.assert_called_once()


@pytest.mark.asyncio
async def test_market_snapshot_quick_and_last_close_both_fail():
    """quick 与 last-close 均失败 → degraded，reason 说明当前与最近交易日均不可用。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=None)
        mock_api.get_last_close_snapshot = AsyncMock(return_value=None)
        mock_to_thread.side_effect = RuntimeError("yfinance unavailable")

        ev = await market_snapshot(
            {"scope": "a_share", "snapshot_kind": "quick"},
            _goal(),
        )

    assert ev.degraded is True
    assert "A 股" in (ev.degraded_reason or "")
    assert len(ev.sources) == 0
    mock_api.get_last_close_snapshot.assert_called_once()


# ── Test 9: scope=both，A 股 last-close 成功、global 失败 → per-source 降级语义 ─


@pytest.mark.asyncio
async def test_market_snapshot_both_last_close_ok_global_fails():
    """scope=both：A 股 last-close 成功、global 失败 → degraded=True，facts 仍含 A 股真实数据。"""
    from aistock_agent.skills.market_snapshot import market_snapshot

    with (
        patch("aistock_agent.skills.market_snapshot.node_api") as mock_api,
        patch(
            "aistock_agent.skills.market_snapshot.asyncio.to_thread",
        ) as mock_to_thread,
    ):
        mock_api.get_quick_snapshot = AsyncMock(return_value=None)
        mock_api.get_last_close_snapshot = AsyncMock(return_value=LAST_CLOSE_OK)
        mock_to_thread.side_effect = RuntimeError("yfinance unavailable")

        ev = await market_snapshot(
            {"scope": "both", "snapshot_kind": "quick"},
            _goal(),
        )

    # degraded 为整体标志：global 缺失 → True
    assert ev.degraded is True
    assert "全球" in (ev.degraded_reason or "")
    # facts 仍含 A 股真实数据（per-source 语义：A 股不被 global 拖累）
    fact_text = " ".join(ev.facts)
    assert "上证指数" in fact_text
    assert "3804.69" in fact_text
    # A 股 source 标注最近交易日 trade_date
    a_share_sources = [s for s in ev.sources if s.source_id.startswith("market:a_share:")]
    assert len(a_share_sources) == 1
    assert "2026-07-31" in a_share_sources[0].title or "20260731" in a_share_sources[0].source_id
    assert ev.raw.get("a_share_success") is True
    assert ev.raw.get("global_success") is False
