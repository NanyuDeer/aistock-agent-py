"""monitor_tools 测试 — 个股监控与告警历史"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.monitor_tools import get_alert_history, get_stock_monitor


# ── get_stock_monitor ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_stock_monitor_success():
    """get_stock_monitor 正常返回个股研判资讯事件列表"""
    mock_data = [
        {
            "event_id": "stock_info:123",
            "stock_name": "贵州茅台",
            "change_type_name": "公告研判",
            "level": "利好",
            "cycle": "short",
            "title": "贵州茅台发布2026年半年报",
            "summary": "营收同比增长15%，净利润增长18%",
            "event_time": "2026-07-08 08:30",
        },
    ]
    with patch("aistock_agent.tools.monitor_tools.node_api") as mock_api:
        mock_api.get_list = AsyncMock(return_value=mock_data)
        result = await get_stock_monitor.ainvoke({"symbol": "600519"})
        assert "贵州茅台" in result
        assert "半年报" in result
        mock_api.get_list.assert_called_once_with("/internal/monitor/600519")


@pytest.mark.asyncio
async def test_get_stock_monitor_degradation():
    """get_stock_monitor 异常时返回降级文本"""
    with patch("aistock_agent.tools.monitor_tools.node_api") as mock_api:
        mock_api.get_list = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_stock_monitor.ainvoke({"symbol": "600519"})
        assert result == DEGRADED_MESSAGE


# ── get_alert_history ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_alert_history_success():
    """get_alert_history 正常返回全局告警历史"""
    mock_data = {
        "total": 2,
        "events": [
            {
                "event_id": "stock_info:456",
                "stock_name": "宁德时代",
                "change_type_name": "新闻研判",
                "level": "重大利好",
                "title": "宁德时代发布固态电池突破",
                "event_time": "2026-07-08 10:00",
            },
        ],
    }
    with patch("aistock_agent.tools.monitor_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_alert_history.ainvoke({"days": 7})
        assert "宁德时代" in result
        assert "固态电池" in result
        mock_api.get.assert_called_once_with("/internal/monitor/alerts?days=7")


@pytest.mark.asyncio
async def test_get_alert_history_symbol_filtering():
    """get_alert_history 传入 symbol 时客户端过滤只返回对应股票的事件

    覆盖 stock_code / symbol 两种字段匹配，确保过滤分支被实际执行。
    """
    mock_data = {
        "total": 3,
        "events": [
            {
                "event_id": "stock_info:300750-1",
                "stock_code": "300750",
                "stock_name": "宁德时代",
                "change_type_name": "新闻研判",
                "level": "重大利好",
                "title": "宁德时代发布固态电池突破",
                "event_time": "2026-07-08 10:00",
            },
            {
                "event_id": "stock_info:600519-1",
                "stock_code": "600519",
                "stock_name": "贵州茅台",
                "change_type_name": "公告研判",
                "level": "利好",
                "title": "贵州茅台发布2026年半年报",
                "event_time": "2026-07-08 08:30",
            },
            {
                "event_id": "stock_info:300750-2",
                "symbol": "300750",
                "stock_name": "宁德时代",
                "change_type_name": "异动研判",
                "level": "关注",
                "title": "宁德时代股价异动",
                "event_time": "2026-07-08 11:00",
            },
        ],
    }
    with patch("aistock_agent.tools.monitor_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_alert_history.ainvoke({"symbol": "300750", "days": 7})
        # 只包含 300750 的事件，不包含 600519
        assert "宁德时代" in result
        assert "固态电池" in result
        assert "股价异动" in result
        assert "贵州茅台" not in result
        assert "半年报" not in result
        # symbol 仅用于客户端过滤，不透传给 Node.js；days 透传
        mock_api.get.assert_called_once_with("/internal/monitor/alerts?days=7")


@pytest.mark.asyncio
async def test_alert_history_degradation():
    """get_alert_history 异常时返回降级文本"""
    with patch("aistock_agent.tools.monitor_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_alert_history.ainvoke({"days": 7})
        assert result == DEGRADED_MESSAGE
