"""sector_tools 测试"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.sector_tools import (
    _format_wind_leaders,
    get_leader_stocks,
    get_wind_leaders,
)


@pytest.mark.asyncio
async def test_get_leader_stocks_success():
    """get_leader_stocks 正常返回龙头股"""
    mock_data = {
        "tag_name": "白酒",
        "leaders": [
            {"name": "贵州茅台", "code": "600519", "change_pct": 2.5, "reason": "业绩超预期"},
            {"name": "五粮液", "code": "000858", "change_pct": 1.8, "reason": "北向资金流入"},
        ],
    }
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_leader_stocks.ainvoke({"tag_code": "BK0475"})
        assert "白酒" in result
        assert "贵州茅台" in result
        mock_api.get.assert_called_once_with("/internal/leader/BK0475")


@pytest.mark.asyncio
async def test_get_leader_stocks_empty():
    """get_leader_stocks 空数据"""
    mock_data = {"tag_name": "白酒", "leaders": []}
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_leader_stocks.ainvoke({"tag_code": "BK0475"})
        assert "暂无龙头股" in result


# ── get_wind_leaders ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_wind_leaders_success():
    """get_wind_leaders 正常返回风口龙头板块及龙头股"""
    mock_data = {
        "update_time": "2026-07-08 09:30",
        "hot_sectors": [
            {
                "name": "半导体",
                "today_change": 3.2,
                "leading_stock": "中芯国际",
                "leading_change": 8.5,
                "main_stocks": [
                    {"code": "688981", "name": "中芯国际", "change_pct": 8.5, "reason": "国产替代加速"},
                ],
            },
        ],
    }
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_wind_leaders.ainvoke({})
        assert "半导体" in result
        assert "中芯国际" in result
        mock_api.get.assert_called_once_with("/internal/wind-leaders")


@pytest.mark.asyncio
async def test_get_wind_leaders_degradation():
    """get_wind_leaders 异常时返回降级文本"""
    with patch("aistock_agent.tools.sector_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_wind_leaders.ainvoke({})
        assert result == DEGRADED_MESSAGE


# ── _format_wind_leaders（短长线分类标注）──────────────────────────


def test_format_wind_leaders_cycle_label():
    """验证 _format_wind_leaders 输出含 [长线风口]/[短线风口] 标注，缺省 cycle 兜底短线"""
    data = {
        "update_time": "2026-08-04 09:00",
        "hot_sectors": [
            {"name": "人工智能", "cycle": "long", "today_change": 3.2, "leading_stock": "科大讯飞"},
            {"name": "白酒", "cycle": "short", "today_change": 1.1, "leading_stock": "贵州茅台"},
            {"name": "无cycle字段板块", "today_change": 0.5, "leading_stock": "-"},
        ],
    }
    text = _format_wind_leaders(data)
    assert "[长线风口]" in text
    assert "[短线风口]" in text
    assert "无cycle字段板块[短线风口]" in text  # 缺省兜底 short
