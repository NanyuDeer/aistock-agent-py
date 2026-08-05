"""market_snapshot raw.a_share_card 结构化字段单测（P11 线 3，spec §3.1/§3.4）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.skills.market_snapshot import _build_a_share_card, market_snapshot

# Node quick snapshot（腾讯行情，无需 coverage 校验）；indexes 含 6 条指数
QUICK_SNAPSHOT_OK: dict[str, object] = {
    "schema_version": "1.0",
    "status": "complete",
    "snapshot_kind": "quick",
    "trade_date": "20260805",
    "captured_at": "2026-08-05T07:30:00.000Z",
    "indexes": [
        {"ts_code": "000001.SH", "name": "上证指数", "close": 3200.0, "pct_chg": 0.5,
         "amount": 100000.0},
        {"ts_code": "399001.SZ", "name": "深证成指", "close": 10500.0, "pct_chg": -0.3,
         "amount": 120000.0},
        {"ts_code": "399006.SZ", "name": "创业板指", "close": 2100.0, "pct_chg": 0.3,
         "amount": 50000.0},
        {"ts_code": "000300.SH", "name": "沪深300", "close": 3800.0, "pct_chg": 0.2,
         "amount": 80000.0},
        {"ts_code": "000905.SH", "name": "中证500", "close": 5500.0, "pct_chg": 0.1,
         "amount": 60000.0},
        {"ts_code": "000852.SH", "name": "中证1000", "close": 6000.0, "pct_chg": 0.05,
         "amount": 70000.0},
    ],
    "breadth": {"total_count": 5000, "advance_count": 2500, "decline_count": 2000,
                "flat_count": 500, "advance_ratio": 0.5},
    "turnover": {"amount_yuan": 95_000_000_000, "previous_amount_yuan": 90_000_000_000,
                 "change_pct": 5.0},
}


def _goal() -> InsightGoal:
    return InsightGoal(question="大盘怎么样", intent="report_lookup")


def test_build_a_share_card_from_normalized():
    """normalize_a_share 输出 → a_share_card（6 指数 + 涨跌家数 + trade_date）。"""
    from aistock_agent.services.market_trace_snapshot import normalize_a_share

    normalized = normalize_a_share(QUICK_SNAPSHOT_OK)
    card = _build_a_share_card(normalized, "20260805")
    assert card is not None
    assert len(card["indices"]) == 6
    first = card["indices"][0]
    assert first == {"index_name": "上证指数", "code": "000001.SH",
                     "value": 3200.0, "change_pct": 0.5}
    assert card["up_count"] == 2500
    assert card["flat_count"] == 500
    assert card["down_count"] == 2000
    assert card["trade_date"] == "20260805"


def test_build_a_share_card_none_without_indexes():
    """无 indexes（只 global 快照）→ 返回 None。"""
    assert _build_a_share_card({"indexes": {}, "breadth": {}}) is None
    assert _build_a_share_card({}) is None


@pytest.mark.asyncio
async def test_market_snapshot_a_share_scope_produces_card():
    """scope=a_share, quick → raw.a_share_card 存在。"""

    with patch("aistock_agent.skills.market_snapshot.node_api") as mock_api:
        mock_api.get_quick_snapshot = AsyncMock(return_value=QUICK_SNAPSHOT_OK)

        ev = await market_snapshot({"scope": "a_share", "snapshot_kind": "quick"}, _goal())

    assert ev.skill_name == "market_snapshot"
    card = ev.raw.get("a_share_card")
    assert isinstance(card, dict)
    assert len(card["indices"]) == 6
    assert card["trade_date"] == "20260805"


@pytest.mark.asyncio
async def test_market_snapshot_global_scope_no_a_share_card():
    """scope=global（无 a_share）→ raw 无 a_share_card。"""
    with (
        patch("aistock_agent.skills.market_snapshot.node_api"),
        patch("aistock_agent.skills.market_snapshot.asyncio.to_thread") as mock_to_thread,
    ):
        mock_to_thread.side_effect = lambda fn, arg: [  # noqa: ARG005
            {"ticker": "^GSPC", "name": "标普500", "price": 5500.0, "change_pct": 0.36},
        ]
        ev = await market_snapshot({"scope": "global", "snapshot_kind": "quick"}, _goal())

    assert "a_share_card" not in ev.raw
    assert ev.raw["scope"] == "global"
