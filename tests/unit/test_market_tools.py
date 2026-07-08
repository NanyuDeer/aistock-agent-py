"""market_tools 测试 — mock yfinance"""

from unittest.mock import MagicMock, patch

import pytest

from aistock_agent.tools.market_tools import get_global_markets


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
