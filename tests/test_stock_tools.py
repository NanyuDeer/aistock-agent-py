"""stock_tools 测试 — mock Node.js API 调用"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote


@pytest.mark.asyncio
async def test_get_quote_success():
    """get_quote 正常返回行情数据（腾讯中文 key）"""
    mock_data = {
        "股票代码": "600519",
        "股票简称": "贵州茅台",
        "最新价": 1688.00,
        "涨跌幅": 0.75,
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
    """get_capital_flow 正常返回资金流向（新浪 r0_* 字段）"""
    mock_data = {
        "r0_in": 963800881.64,
        "r0_out": 1438252421.35,
        "netamount": -707356409.68,
        "name": "贵州茅台",
    }
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_capital_flow.ainvoke({"symbol": "600519"})
        assert "963800881" in result
        assert "-707356409" in result
        mock_api.get.assert_called_once_with("/internal/flow/600519")


@pytest.mark.asyncio
async def test_get_profit_forecast_success():
    """get_profit_forecast 正常返回盈利预测（同花顺摘要 + 详细表）"""
    mock_data = {
        "摘要": "截至2026-07-06，共有 46 家机构对贵州茅台的2026年度业绩作出预测；预测2026年每股收益 68.82 元",
        "业绩预测详表_详细指标预测": [
            {"预测指标": "净利润(元)", "预测2026-平均": "861.83亿", "预测2027-平均": "909.21亿"},
        ],
    }
    with patch("aistock_agent.tools.stock_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_profit_forecast.ainvoke({"symbol": "600519"})
        assert "2026" in result
        assert "68.82" in result
        assert "861.83亿" in result
        mock_api.get.assert_called_once_with("/internal/forecast/600519")
