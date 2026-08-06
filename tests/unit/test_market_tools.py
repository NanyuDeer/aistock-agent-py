"""market_tools 测试 — mock node_api（腾讯行情源，经 app-api /api/gb/index/quotes）"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.market_tools import (
    GLOBAL_MARKET_SYMBOLS,
    collect_global_market_facts,
    get_global_markets,
)


def _quote(
    symbol: str, name: str, price: float | None, change_pct: float | None
) -> dict[str, object]:
    return {
        "指数代码": symbol,
        "指数简称": name,
        "最新价": price,
        "涨跌幅": change_pct,
        "涨跌额": 0.0,
    }


QUOTES: list[dict[str, object]] = [
    _quote("SPX", "标普500", 5500.0, 0.36),
    _quote("IXIC", "纳斯达克综合指数", 18000.0, 0.5),
    _quote("DJI", "道琼斯工业指数", 40000.0, -0.2),
    _quote("GOLD", "纽约黄金", 4218.7, 1.61),
    _quote("CRUDE", "纽约原油", 76.27, 0.65),
    _quote("USDCNY", "美元人民币", 6.7486, 0.02),
]


def _payload(quotes: list[dict[str, object]] | None = None) -> dict[str, object]:
    items = quotes if quotes is not None else QUOTES
    return {"来源": "Tencent", "指数数量": len(items), "行情": items}


@pytest.mark.asyncio
async def test_get_global_markets_success():
    """get_global_markets 正常返回全球市场数据（含指数与大宗/汇率）。"""
    with patch(
        "aistock_agent.tools.market_tools.node_api.get",
        new=AsyncMock(return_value=_payload()),
    ):
        result = await get_global_markets.ainvoke({})
    assert "标普500" in result
    assert "纽约黄金" in result


@pytest.mark.asyncio
async def test_collect_global_market_facts_returns_structured_facts():
    """collect_global_market_facts 返回结构化事实列表。"""
    captured_at = datetime(2026, 8, 5, 7, 31, 0, tzinfo=UTC)
    with patch(
        "aistock_agent.tools.market_tools.node_api.get",
        new=AsyncMock(return_value=_payload()),
    ):
        facts = await collect_global_market_facts(captured_at)

    assert len(facts) == len(QUOTES)
    assert facts[0]["ticker"] == "SPX"
    assert facts[0]["name"] == "标普500"
    assert facts[0]["price"] == 5500.0
    assert facts[0]["change_pct"] == 0.36
    assert facts[0]["observed_at"] == captured_at.isoformat()


@pytest.mark.asyncio
async def test_collect_global_market_facts_skips_no_price():
    """行情缺最新价时跳过，不报错。"""
    quotes = [_quote("SPX", "标普500", None, 0.36)]
    captured_at = datetime(2026, 8, 5, 7, 31, 0, tzinfo=UTC)
    with patch(
        "aistock_agent.tools.market_tools.node_api.get",
        new=AsyncMock(return_value=_payload(quotes)),
    ):
        facts = await collect_global_market_facts(captured_at)
    assert len(facts) == 0


@pytest.mark.asyncio
async def test_collect_global_market_facts_raises_on_unavailable():
    """Node 接口不可用（None）时抛异常，供上层按 unavailable 处理。"""
    with patch(
        "aistock_agent.tools.market_tools.node_api.get",
        new=AsyncMock(return_value=None),
    ):
        with pytest.raises(Exception):
            await collect_global_market_facts(datetime.now(UTC))


def test_global_market_symbols_cover_core_markets():
    """GLOBAL_MARKET_SYMBOLS 覆盖美股/港股/大宗/汇率。"""
    assert "SPX" in GLOBAL_MARKET_SYMBOLS
    assert "IXIC" in GLOBAL_MARKET_SYMBOLS
    assert "DJI" in GLOBAL_MARKET_SYMBOLS
    assert "HXC" in GLOBAL_MARKET_SYMBOLS
    assert "HSI" in GLOBAL_MARKET_SYMBOLS
    assert "HSTECH" in GLOBAL_MARKET_SYMBOLS
    assert "GOLD" in GLOBAL_MARKET_SYMBOLS
    assert "CRUDE" in GLOBAL_MARKET_SYMBOLS
    assert "USDCNY" in GLOBAL_MARKET_SYMBOLS
