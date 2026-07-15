"""trend_tools 测试 — 趋势股评分、详情与 Top 列表"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.trend_tools import (
    get_trend_score,
    get_trend_score_detail,
    get_trend_top_stocks,
)


# ── get_trend_score ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_trend_score_success():
    """get_trend_score 正常返回趋势股评分结果"""
    mock_data = {
        "score": 82.5,
        "label": "A",
        "expectedMultiple": "3-5倍",
        "description": "综合评分82.5分（A级），具备趋势股特征",
        "aiConclusion": "K线走出一倍趋势，60日线上方运行",
        "dimensions": [
            {"name": "技术面", "weight": 35, "score": 90},
            {"name": "行业赛道景气", "weight": 25, "score": 78},
            {"name": "消息面催化", "weight": 20, "score": 75},
            {"name": "基本面", "weight": 20, "score": 80},
        ],
        "updatedAt": "2026-07-15T09:00:00Z",
    }
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_trend_score.ainvoke({"symbol": "600519"})
        assert "82" in result
        assert "A" in result
        assert "3-5倍" in result
        mock_api.get.assert_called_once_with("/internal/trend/score/600519")


@pytest.mark.asyncio
async def test_get_trend_score_not_found():
    """get_trend_score 数据不存在时返回未找到提示"""
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=None)
        result = await get_trend_score.ainvoke({"symbol": "999999"})
        assert "未找到" in result
        assert "999999" in result


@pytest.mark.asyncio
async def test_get_trend_score_degradation():
    """get_trend_score 异常时返回降级文本"""
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_trend_score.ainvoke({"symbol": "600519"})
        assert result == DEGRADED_MESSAGE


# ── get_trend_score_detail ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_trend_score_detail_success():
    """get_trend_score_detail 正常返回趋势股评分详情"""
    mock_data = {
        "score": 82.5,
        "label": "A",
        "expectedMultiple": "3-5倍",
        "dimensions": [
            {
                "name": "技术面",
                "weight": 35,
                "score": 90,
                "detail": {
                    "kline": {"dates": ["2026-01-01"], "ohlc": [[100, 105, 99, 106]]},
                    "conceptKline": {"name": "白酒", "dates": ["2026-01-01"], "ohlc": [[100, 103, 98, 104]]},
                    "indicators": {
                        "lowPointGain": 95.2,
                        "ma60Position": "above",
                        "ma60Trend": "up",
                        "isNewHigh250": True,
                        "isNewHigh120": True,
                        "maxDrawdown": 8.5,
                    },
                },
            },
            {
                "name": "行业赛道景气",
                "weight": 25,
                "score": 78,
                "detail": {
                    "sectorName": "白酒",
                    "sectorListCount60d": 16,
                    "sectorStrength": "+18.5%",
                    "weeklyListingTrend": [2, 3, 2, 4, 3, 2],
                    "policyItems": [{"name": "消费刺激政策", "desc": "扩大内需", "color": "up"}],
                    "indicators": [],
                },
            },
            {
                "name": "消息面催化",
                "weight": 20,
                "score": 75,
                "detail": {
                    "researchCount": 12,
                    "hardCatalyst": "业绩超预期",
                    "news": [{"title": "贵州茅台业绩超预期", "source": "财联社", "publishTime": "2026-07-14"}],
                    "indicators": [],
                },
            },
            {
                "name": "基本面",
                "weight": 20,
                "score": 80,
                "detail": {
                    "subDimensions": [
                        {"name": "业绩爆发力", "weight": 35, "score": 85, "indicators": []},
                        {"name": "估值弹性", "weight": 25, "score": 78, "indicators": []},
                        {"name": "盈利质量", "weight": 25, "score": 80, "indicators": []},
                        {"name": "竞争壁垒", "weight": 15, "score": 72, "indicators": []},
                    ]
                },
            },
        ],
        "updatedAt": "2026-07-15T09:00:00Z",
    }
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_trend_score_detail.ainvoke({"symbol": "600519"})
        assert "82" in result
        assert "技术面" in result
        assert "白酒" in result
        mock_api.get.assert_called_once_with("/internal/trend/score/600519/detail")


@pytest.mark.asyncio
async def test_get_trend_score_detail_not_found():
    """get_trend_score_detail 数据不存在时返回未找到提示"""
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=None)
        result = await get_trend_score_detail.ainvoke({"symbol": "999999"})
        assert "未找到" in result


@pytest.mark.asyncio
async def test_get_trend_score_detail_degradation():
    """get_trend_score_detail 异常时返回降级文本"""
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_trend_score_detail.ainvoke({"symbol": "600519"})
        assert result == DEGRADED_MESSAGE


# ── get_trend_top_stocks ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_trend_top_stocks_success():
    """get_trend_top_stocks 正常返回趋势股 Top 列表"""
    mock_data = [
        {"symbol": "600519", "name": "贵州茅台", "score": 85.0, "label": "S", "expectedMultiple": "5-10倍", "industry": "白酒"},
        {"symbol": "300750", "name": "宁德时代", "score": 80.2, "label": "A", "expectedMultiple": "3-5倍", "industry": "电池"},
    ]
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get_list = AsyncMock(return_value=mock_data)
        result = await get_trend_top_stocks.ainvoke({"limit": 20})
        assert "贵州茅台" in result
        assert "宁德时代" in result
        mock_api.get_list.assert_called_once_with("/internal/trend/top?limit=20")


@pytest.mark.asyncio
async def test_get_trend_top_stocks_empty():
    """get_trend_top_stocks 列表为空时返回提示"""
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get_list = AsyncMock(return_value=None)
        result = await get_trend_top_stocks.ainvoke({"limit": 20})
        assert "暂无" in result


@pytest.mark.asyncio
async def test_get_trend_top_stocks_degradation():
    """get_trend_top_stocks 异常时返回降级文本"""
    with patch("aistock_agent.tools.trend_tools.node_api") as mock_api:
        mock_api.get_list = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_trend_top_stocks.ainvoke({"limit": 20})
        assert result == DEGRADED_MESSAGE
