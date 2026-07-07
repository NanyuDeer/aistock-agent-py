"""news_tools 测试"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.news_tools import get_news_fulltext, search_cls_news


@pytest.mark.asyncio
async def test_search_cls_news_success():
    """search_cls_news 正常返回新闻"""
    mock_data = {
        "items": [
            {"title": "贵州茅台发布年报", "time": "2026-07-04 08:30", "brief": "营收增长15%", "id": "12345"},
        ],
    }
    with patch("aistock_agent.tools.news_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await search_cls_news.ainvoke({"symbol": "600519"})
        assert "贵州茅台" in result
        mock_api.get.assert_called_once_with("/internal/news/search/600519")


@pytest.mark.asyncio
async def test_get_news_fulltext_success():
    """get_news_fulltext 正常返回全文"""
    mock_data = {"title": "贵州茅台年报", "content": "2025年营收突破1500亿"}
    with patch("aistock_agent.tools.news_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_news_fulltext.ainvoke({"news_id": "12345"})
        assert "贵州茅台年报" in result
        assert "1500亿" in result
        mock_api.get.assert_called_once_with("/internal/news/fulltext/12345")


@pytest.mark.asyncio
async def test_search_cls_news_no_results():
    """search_cls_news 无结果"""
    with patch("aistock_agent.tools.news_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=None)
        result = await search_cls_news.ainvoke({"symbol": "600519"})
        assert "未找到" in result
