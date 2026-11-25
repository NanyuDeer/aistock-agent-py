"""hot_burst_tools 测试 — 机构调研推荐热门股检测与历史"""

from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.hot_burst_tools import (
    get_hot_burst,
    get_hot_burst_history,
    get_hot_burst_report_history,
)

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
async def test_get_hot_burst_uses_18_hour_default_window():
    """未显式指定时，Agent 工具应覆盖早间检测与定时报告之间的时间差。"""
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"outbreaks": []})
        await get_hot_burst.ainvoke({})

    mock_api.get.assert_called_once_with(
        "/internal/institution-research?hours=18&min_resonance_count=2&limit=20"
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


@pytest.mark.asyncio
async def test_get_hot_burst_history_supports_min_resonance():
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"records": []})
        await get_hot_burst_history.ainvoke(
            {"limit": 50, "days": 1, "min_resonance": 2, "offset": 0}
        )
        mock_api.get.assert_called_once_with(
            "/internal/institution-research/history?limit=50&min_resonance_only=true&days=1&min_resonance=2&offset=0"
        )


def test_history_format_deduplicates_before_display_limit():
    from aistock_agent.tools.hot_burst_tools import _format_history
    rows = [{"symbol": "300308", "stock_name": "中际旭创", "detected_at": "最新"}]
    rows += [{"symbol": "300308", "stock_name": "中际旭创", "detected_at": "更早"}] * 25
    rows += [{"symbol": "300124", "stock_name": "汇川技术"}]
    result = _format_history({"total": 27, "records": rows})
    assert result.count("中际旭创(300308)") == 1
    assert "汇川技术(300124)" in result
    assert "历史记录总数: 27" in result
    assert "本页去重股票数: 2" in result


@pytest.mark.asyncio
async def test_report_tool_always_requests_one_day_and_two_sources():
    assert "days" not in get_hot_burst_report_history.args
    assert "min_resonance" not in get_hot_burst_report_history.args
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"total": 0, "records": []})
        for args in ({}, {"limit": 100, "offset": 50}, {"days": 30}):
            await get_hot_burst_report_history.ainvoke(args)
            query = parse_qs(urlsplit(mock_api.get.call_args.args[0]).query)
            assert query["days"] == ["1"]
            assert query["min_resonance"] == ["2"]
            assert query["min_resonance_only"] == ["true"]
        assert query["limit"] == ["50"]


@pytest.mark.asyncio
async def test_generic_history_tool_still_allows_longer_window():
    with patch("aistock_agent.tools.hot_burst_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"total": 0, "records": []})
        await get_hot_burst_history.ainvoke({"days": 30})
        assert "days=30" in mock_api.get.call_args.args[0]
