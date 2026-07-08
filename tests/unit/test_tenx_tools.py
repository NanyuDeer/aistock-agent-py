"""tenx_tools 测试 — 十倍股评分与 Top 列表"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.tenx_tools import get_tenx_score, get_tenx_top_stocks


# ── get_tenx_score ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_tenx_score_success():
    """get_tenx_score 正常返回十倍股评分结果"""
    mock_data = {
        "score": 85.2,
        "label": "S",
        "expectedMultiple": "5-10倍",
        "description": "综合评分85.2分（S级），具备十倍股核心特征",
        "aiConclusion": "贵州茅台十倍股评分85.2分(S级)",
        "dimensions": [
            {"name": "业绩爆发力", "weight": 30, "score": 88,
             "indicators": [{"name": "未来2年预期净利润复合增速", "key": "profit_forecast_cagr",
                             "value": "85.0%", "score": 100}]},
        ],
        "updatedAt": "2026-07-08T09:00:00Z",
    }
    with patch("aistock_agent.tools.tenx_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_tenx_score.ainvoke({"symbol": "600519"})
        assert "85" in result
        assert "S" in result
        mock_api.get.assert_called_once_with("/internal/tenx/score/600519")


@pytest.mark.asyncio
async def test_get_tenx_score_degradation():
    """get_tenx_score 异常时返回降级文本"""
    with patch("aistock_agent.tools.tenx_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_tenx_score.ainvoke({"symbol": "600519"})
        assert result == DEGRADED_MESSAGE


# ── get_tenx_top_stocks ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_tenx_top_stocks_success():
    """get_tenx_top_stocks 正常返回十倍股 Top 列表"""
    mock_data = {
        "stocks": [
            {"symbol": "300059", "name": "东方财富", "score": 85.2, "label": "S"},
            {"symbol": "600519", "name": "贵州茅台", "score": 82.1, "label": "S"},
        ],
        "note": "stub: batch tenx score not yet implemented",
    }
    with patch("aistock_agent.tools.tenx_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_tenx_top_stocks.ainvoke({"limit": 20})
        assert "东方财富" in result
        assert "贵州茅台" in result
        mock_api.get.assert_called_once_with("/internal/tenx/top?limit=20")


@pytest.mark.asyncio
async def test_get_tenx_top_stocks_degradation():
    """get_tenx_top_stocks 异常时返回降级文本"""
    with patch("aistock_agent.tools.tenx_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_tenx_top_stocks.ainvoke({"limit": 20})
        assert result == DEGRADED_MESSAGE
