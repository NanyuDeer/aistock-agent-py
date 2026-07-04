"""stock_tools 测试 — mock Node.js API 调用"""

import pytest
from unittest.mock import AsyncMock, patch

from aistock_agent.tools.stock_tools import get_quote, get_capital_flow, get_profit_forecast


@pytest.mark.asyncio
async def test_get_quote_success():
    """get_quote 正常返回行情数据"""
    mock_data = {
        "name": "贵州茅台",
        "price": 1688.00,
        "change": 12.50,
        "change_pct": 0.75,
        "volume": "2.3万手",
        "turnover": "38.8亿",
    }
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_quote.ainvoke({"symbol": "600519"})
        assert "贵州茅台" in result
        assert "1688" in result
        mock_api.get.assert_called_once_with("/internal/quote/600519")


@pytest.mark.asyncio
async def test_get_quote_not_found():
    """get_quote 找不到数据"""
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=None)
        result = await get_quote.ainvoke({"symbol": "999999"})
        assert "未找到" in result


@pytest.mark.asyncio
async def test_get_capital_flow_success():
    """get_capital_flow 正常返回资金流向"""
    mock_data = {
        "main_inflow": "5.2亿",
        "main_outflow": "3.8亿",
        "main_net": "1.4亿",
    }
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_capital_flow.ainvoke({"symbol": "600519"})
        assert "5.2亿" in result
        assert "1.4亿" in result
        mock_api.get.assert_called_once_with("/internal/flow/600519")


@pytest.mark.asyncio
async def test_get_profit_forecast_success():
    """get_profit_forecast 正常返回盈利预测"""
    mock_data = {
        "year": "2026",
        "eps_forecast": "68.5",
        "rating": "买入",
        "org_count": "28",
    }
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_profit_forecast.ainvoke({"symbol": "600519"})
        assert "2026" in result
        assert "68.5" in result
        mock_api.get.assert_called_once_with("/internal/forecast/600519")
