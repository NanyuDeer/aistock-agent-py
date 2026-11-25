"""performance_tools 测试 — mock Node.js API 调用"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.performance_tools import (
    get_latest_performance_reports,
    get_performance_report,
)
from aistock_agent.tools.registry import get_tools


@pytest.mark.asyncio
async def test_get_performance_report_success():
    """get_performance_report 正常返回多期业绩报告（元 → 亿元格式化）"""
    mock_data = {
        "symbol": "600519",
        "stock_name": "贵州茅台",
        "reports": [
            {
                "report_type": "formal",
                "report_type_label": "正式报告",
                "ann_date": "20260826",
                "end_date": "20260630",
                "total_revenue": 81932608406.61,
                "n_income": 41696301342.59,
                "n_income_attr_p": 41696301342.59,
                "basic_eps": 33.19,
                "summary": "",
                "ai_tag": "业绩大幅增长",
            },
            {
                "report_type": "express",
                "report_type_label": "业绩快报",
                "ann_date": "20260812",
                "end_date": "20260630",
                "total_revenue": 81932608406.61,
                "n_income": 41696301342.59,
                "n_income_attr_p": 41696301342.59,
                "basic_eps": 33.19,
                "summary": "",
                "ai_tag": "",
            },
        ],
    }
    with patch("aistock_agent.tools.performance_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_performance_report.ainvoke({"symbol": "600519"})
        assert "贵州茅台" in result
        assert "600519" in result
        assert "2026半年报" in result
        assert "正式报告" in result
        assert "业绩快报" in result
        assert "819.33 亿元" in result  # 81932608406.61 / 1e8
        assert "416.96 亿元" in result
        assert "33.19 元" in result
        assert "业绩大幅增长" in result
        mock_api.get.assert_called_once_with("/internal/performance-report/600519")


@pytest.mark.asyncio
async def test_get_performance_report_not_found():
    """get_performance_report 无数据（接口 404 → node_api 返回 None）"""
    with patch("aistock_agent.tools.performance_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=None)
        result = await get_performance_report.ainvoke({"symbol": "999999"})
        assert "未找到" in result


@pytest.mark.asyncio
async def test_get_latest_performance_reports_success():
    """get_latest_performance_reports 正常返回最新披露列表（默认 formal）"""
    mock_data = {
        "reports": [
            {
                "symbol": "600519",
                "stock_name": "贵州茅台",
                "report_type": "formal",
                "report_type_label": "正式报告",
                "ann_date": "20260826",
                "end_date": "20260630",
                "total_revenue": 81932608406.61,
                "n_income_attr_p": 41696301342.59,
                "basic_eps": 33.19,
                "revenue_yoy": 15.20,
                "profit_yoy": 10.10,
                "summary": "",
                "ai_tag": "",
            },
            {
                "symbol": "000001",
                "stock_name": "平安银行",
                "report_type": "formal",
                "report_type_label": "正式报告",
                "ann_date": "20260826",
                "end_date": "20260630",
                "total_revenue": 81932608406.61,
                "n_income_attr_p": 41696301342.59,
                "basic_eps": 2.15,
                "revenue_yoy": None,
                "profit_yoy": -3.50,
                "summary": "",
                "ai_tag": "",
            },
        ]
    }
    with patch("aistock_agent.tools.performance_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_latest_performance_reports.ainvoke({"report_type": "formal", "limit": 10})
        assert "正式报告" in result
        assert "贵州茅台" in result
        assert "平安银行" in result
        assert "+10.1%" in result  # 净利同比
        assert "-3.5%" in result
        mock_api.get.assert_called_once_with(
            "/internal/performance-reports/latest?reportType=formal&limit=10"
        )


@pytest.mark.asyncio
async def test_get_latest_performance_reports_empty():
    """get_latest_performance_reports 空列表"""
    with patch("aistock_agent.tools.performance_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value={"reports": []})
        result = await get_latest_performance_reports.ainvoke({"report_type": "express", "limit": 5})
        assert "暂无" in result


@pytest.mark.asyncio
async def test_registered_to_stock_and_general():
    """工具已注册到 stock / general 分类（registry 自动加载）"""
    stock_tools = {t.name for t in get_tools("stock")}
    general_tools = {t.name for t in get_tools("general")}
    assert "get_performance_report" in stock_tools
    assert "get_performance_report" in general_tools
    assert "get_latest_performance_reports" in general_tools
