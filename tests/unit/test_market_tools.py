"""market_tools 测试 — mock yfinance"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from aistock_agent.tools.market_tools import collect_global_market_facts, get_global_markets


@pytest.mark.asyncio
async def test_get_global_markets_success():
    """get_global_markets 正常返回全球市场数据"""
    # yfinance 返回 mock 结构
    mock_ticker = MagicMock()
    mock_info = MagicMock()
    mock_info.last_price = 5500.0
    mock_info.previous_close = 5480.0
    mock_info.regular_market_change = 20.0
    mock_info.regular_market_change_percent = 0.36
    mock_ticker.fast_info = mock_info

    mock_yf = MagicMock()
    mock_tickers = MagicMock()
    mock_tickers.tickers = {"^GSPC": mock_ticker, "^IXIC": mock_ticker, "^DJI": mock_ticker}
    mock_yf.Tickers.return_value = mock_tickers

    with patch("aistock_agent.tools.market_tools.yf", mock_yf):
        result = await get_global_markets.ainvoke({})
        assert "标普500" in result


def test_collect_global_market_facts_returns_structured_facts():
    """collect_global_market_facts 返回结构化事实列表。"""
    mock_ticker = MagicMock()
    mock_info = MagicMock()
    mock_info.last_price = 5500.0
    mock_info.previous_close = 5480.0
    mock_info.regular_market_change_percent = 0.36
    mock_ticker.fast_info = mock_info

    mock_yf = MagicMock()
    mock_tickers = MagicMock()
    mock_tickers.tickers = {"^GSPC": mock_ticker, "^IXIC": mock_ticker, "^DJI": mock_ticker}
    mock_yf.Tickers.return_value = mock_tickers

    captured_at = datetime(2026, 7, 19, 7, 31, 0, tzinfo=timezone.utc)
    with patch("aistock_agent.tools.market_tools.yf", mock_yf):
        facts = collect_global_market_facts(captured_at)

    assert len(facts) == 3  # sp500, nasdaq, dow
    assert facts[0]["ticker"] == "^GSPC"
    assert facts[0]["name"] == "标普500"
    assert facts[0]["price"] == 5500.0
    assert facts[0]["change_pct"] == 0.36
    assert facts[0]["observed_at"] == captured_at.isoformat()


def test_collect_global_market_facts_skips_missing_ticker():
    """yfinance 缺少某 ticker 时跳过，不报错。"""
    mock_ticker = MagicMock()
    mock_info = MagicMock()
    mock_info.last_price = 5500.0
    mock_info.previous_close = 5480.0
    mock_info.regular_market_change_percent = 0.36
    mock_ticker.fast_info = mock_info

    mock_yf = MagicMock()
    mock_tickers = MagicMock()
    # 只有 ^GSPC，其他 9 个都缺失
    mock_tickers.tickers = {"^GSPC": mock_ticker}
    mock_yf.Tickers.return_value = mock_tickers

    captured_at = datetime(2026, 7, 19, 7, 31, 0, tzinfo=timezone.utc)
    with patch("aistock_agent.tools.market_tools.yf", mock_yf):
        facts = collect_global_market_facts(captured_at)

    assert len(facts) == 1
    assert facts[0]["ticker"] == "^GSPC"


def test_collect_global_market_facts_skips_no_price():
    """ticker 无 price 时跳过。"""
    mock_ticker = MagicMock()
    mock_info = MagicMock()
    mock_info.last_price = None
    mock_info.previous_close = None
    mock_ticker.fast_info = mock_info

    mock_yf = MagicMock()
    mock_tickers = MagicMock()
    mock_tickers.tickers = {"^GSPC": mock_ticker}
    mock_yf.Tickers.return_value = mock_tickers

    captured_at = datetime(2026, 7, 19, 7, 31, 0, tzinfo=timezone.utc)
    with patch("aistock_agent.tools.market_tools.yf", mock_yf):
        facts = collect_global_market_facts(captured_at)

    assert len(facts) == 0
