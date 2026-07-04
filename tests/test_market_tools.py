"""market_tools 测试 — mock yfinance 和 Tavily"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from aistock_agent.tools.market_tools import get_global_markets, tavily_finance_search


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


@pytest.mark.asyncio
async def test_tavily_finance_search_success():
    """tavily_finance_search 正常返回搜索结果"""
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {
        "results": [
            {"title": "美联储维持利率不变", "content": "美联储决定维持当前利率水平...", "url": "https://example.com/1"},
        ]
    }

    with patch("aistock_agent.tools.market_tools.TavilyClient", return_value=mock_tavily_instance):
        result = await tavily_finance_search.ainvoke({"query": "美联储利率决议"})
        assert "美联储" in result


@pytest.mark.asyncio
async def test_tavily_finance_search_no_results():
    """tavily_finance_search 无结果"""
    mock_tavily_instance = MagicMock()
    mock_tavily_instance.search.return_value = {"results": []}

    with patch("aistock_agent.tools.market_tools.TavilyClient", return_value=mock_tavily_instance):
        result = await tavily_finance_search.ainvoke({"query": "测试关键词"})
        assert "未找到" in result
