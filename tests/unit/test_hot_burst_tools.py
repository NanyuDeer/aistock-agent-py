"""hot_burst_tools 测试 — 机构调研推荐热门股检测与历史"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.hot_burst_tools import get_hot_burst, get_hot_burst_history


# ── get_hot_burst ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_hot_burst_success():
    """get_hot_burst 正常返回机构调研推荐热门股检测结果"""
    mock_data = {
        "update_time": "2026-07-08 10:00",
        "total_stocks_checked": 4500,
        "resonance_count": 3,
        "ths_hot_sectors": [{"name": "人工智能", "rank": 1, "change_pct": 5.2}],
        "outbreaks": [
            {
                "symbol": "300308",
                "stockName": "中际旭创",
                "resonanceCount": 4,
                "resonanceLevel": "critical",
                "price": 158.50,
                "changePct": 12.3,
                "sectorInfo": "光模块",
                "articles": [{"title": "中际旭创业绩超预期", "source": "财联社", "time": "2026-07-08"}],
            },
        ],
        "hot_concepts": [],
    }
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_hot_burst.ainvoke({"limit": 20})
        assert "中际旭创" in result
        mock_api.get.assert_called_once_with("/internal/institution-research?limit=20")


@pytest.mark.asyncio
async def test_get_hot_burst_degradation():
    """get_hot_burst 异常时返回降级文本"""
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_hot_burst.ainvoke({"limit": 20})
        assert result == DEGRADED_MESSAGE


# ── get_hot_burst_history ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_hot_burst_history_success():
    """get_hot_burst_history 正常返回历史记录"""
    mock_data = {
        "total": 2,
        "records": [
            {
                "stock_code": "300308",
                "stock_name": "中际旭创",
                "push_date": "2026-07-01",
                "theme": "光模块",
                "resonance_count": 4,
                "push_price": 145.20,
            },
        ],
    }
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_hot_burst_history.ainvoke({"days": 30})
        assert "中际旭创" in result
        mock_api.get.assert_called_once_with(
            "/internal/institution-research/history?days=30")


@pytest.mark.asyncio
async def test_get_hot_burst_history_degradation():
    """get_hot_burst_history 异常时返回降级文本"""
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_hot_burst_history.ainvoke({"days": 30})
        assert result == DEGRADED_MESSAGE
