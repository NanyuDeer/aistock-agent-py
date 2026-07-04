"""sector_tools 测试"""

import pytest
from unittest.mock import AsyncMock, patch

from aistock_agent.tools.sector_tools import get_leader_stocks


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
