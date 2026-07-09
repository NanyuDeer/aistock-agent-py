"""复盘工具测试 — get_market_summary + get_sector_performance"""

from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest


@pytest.mark.asyncio
@patch("aistock_agent.tools.review_tools.yf")
async def test_get_market_summary_success(mock_yf):
    """yfinance 返回 A 股指数数据，格式化输出"""
    from aistock_agent.tools.review_tools import get_market_summary

    # mock yf.Tickers → 每个 ticker.fast_info 返回价格/涨跌
    mock_ticker = MagicMock()
    mock_ticker.fast_info.last_price = 3200.50
    mock_ticker.fast_info.regular_market_change = 15.30
    mock_ticker.fast_info.regular_market_change_percent = 0.48
    mock_tickers = MagicMock()
    mock_tickers.tickers = {"000001.SS": mock_ticker}
    mock_yf.Tickers.return_value = mock_tickers

    result = await get_market_summary.ainvoke({})
    assert "上证指数" in result
    assert "3200" in result


@pytest.mark.asyncio
@patch("aistock_agent.tools.review_tools.yf")
async def test_get_market_summary_partial_failure(mock_yf):
    """部分指数获取失败时，失败的标注"数据暂不可用"，其余正常

    用 PropertyMock 让 ``ticker.fast_info`` 属性访问抛异常（而非 side_effect，
    后者只在 mock 被 *调用* 时触发，属性访问不会触发）。
    用独立子类 ``_FailingTicker`` 挂载 PropertyMock，避免污染共享的 MagicMock
    类型导致其他正常 ticker 也失败。提供全部 4 个 A 股指数 ticker，确保
    走的是"部分失败"路径（fast_info 异常），而非"ticker 缺失"路径。
    """
    from aistock_agent.tools.review_tools import get_market_summary

    mock_ticker_ok = MagicMock()
    mock_ticker_ok.fast_info.last_price = 3200.50
    mock_ticker_ok.fast_info.regular_market_change = 15.30
    mock_ticker_ok.fast_info.regular_market_change_percent = 0.48

    # PropertyMock 必须挂在 mock 的 type 上才能在属性访问时触发；
    # 用独立子类隔离，避免污染共享 MagicMock 类型导致其他 ticker 也失败
    class _FailingTicker(MagicMock):
        pass

    mock_ticker_fail = _FailingTicker()
    type(mock_ticker_fail).fast_info = PropertyMock(
        side_effect=Exception("timeout")
    )

    mock_tickers = MagicMock()
    mock_tickers.tickers = {
        "000001.SS": mock_ticker_ok,   # 上证指数 — 正常
        "399001.SZ": mock_ticker_fail,  # 深证成指 — fast_info 访问抛异常
        "399006.SZ": mock_ticker_ok,   # 创业板指 — 正常
        "000688.SS": mock_ticker_ok,   # 科创50  — 正常
    }
    mock_yf.Tickers.return_value = mock_tickers

    result = await get_market_summary.ainvoke({})
    assert "上证指数" in result
    # 深证成指触发 fast_info 异常 → 标注暂不可用（确认走的是部分失败路径）
    assert "深证成指: 数据暂不可用" in result
    assert "数据暂不可用" in result
    # 其余正常指数应包含价格
    assert "3200" in result


@pytest.mark.asyncio
@patch("aistock_agent.tools.review_tools.node_api")
async def test_get_sector_performance_success(mock_node_api):
    """Node.js 返回板块数据，格式化输出"""
    mock_node_api.get = AsyncMock(return_value={
        "update_time": "2026-07-08 15:00",
        "hot_sectors": [
            {"name": "黄金", "today_change": 3.5, "leading_stock": "山东黄金"},
            {"name": "军工", "today_change": -1.2, "leading_stock": "中航沈飞"},
        ],
    })

    from aistock_agent.tools.review_tools import get_sector_performance
    result = await get_sector_performance.ainvoke({})
    assert "黄金" in result
    assert "军工" in result
    assert "3.5" in result


@pytest.mark.asyncio
@patch("aistock_agent.tools.review_tools.node_api")
async def test_get_sector_performance_empty(mock_node_api):
    """Node.js 返回 None，降级提示"""
    mock_node_api.get = AsyncMock(return_value=None)

    from aistock_agent.tools.review_tools import get_sector_performance
    result = await get_sector_performance.ainvoke({})
    assert "暂无板块涨跌数据" in result
