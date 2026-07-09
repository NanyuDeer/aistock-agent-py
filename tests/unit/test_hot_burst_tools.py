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
        "outbreaks": [
            {
                "symbol": "300308",
                "stockName": "中际旭创",
                "resonanceCount": 4,
                "resonanceLevel": "critical",
                "resonanceScore": 88,
                "price": 158.50,
                "changePct": 12.3,
                "sectorInfo": "光模块",
                "triggerTags": ["光模块", "机构调研", "算力"],
            },
        ],
        "hot_concepts": [],
    }
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_hot_burst.ainvoke(
            {"hours": 6, "min_resonance_count": 2, "limit": 20}
        )
        assert "中际旭创" in result
        mock_api.get.assert_called_once_with(
            "/internal/institution-research?hours=6&min_resonance_count=2&limit=20"
        )


@pytest.mark.asyncio
async def test_get_hot_burst_degradation():
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_hot_burst.ainvoke(
            {"hours": 6, "min_resonance_count": 2, "limit": 20}
        )
        assert result == DEGRADED_MESSAGE


# ── get_hot_burst_history ────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_hot_burst_history_success():
    mock_data = {
        "total": 2,
        "records": [
            {
                "symbol": "300308",
                "stock_name": "中际旭创",
                "detected_at": "2026-07-01T09:30:00Z",
                "resonance_score": 88,
                "resonance_level": "critical",
                "keywords": "光模块、机构调研",
            },
        ],
    }
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_hot_burst_history.ainvoke(
            {"limit": 50, "min_resonance_only": True, "days": 30, "offset": 0}
        )
        assert "中际旭创" in result
        mock_api.get.assert_called_once_with(
            "/internal/institution-research/history?limit=50&min_resonance_only=true&days=30&offset=0"
        )


@pytest.mark.asyncio
async def test_get_hot_burst_history_degradation():
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_hot_burst_history.ainvoke(
            {"limit": 50, "min_resonance_only": True, "days": 30, "offset": 0}
        )
        assert result == DEGRADED_MESSAGE
