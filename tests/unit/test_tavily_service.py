"""Tavily 客户端封装层测试 — mock TavilyClient"""

from unittest.mock import MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_tavily_service_search_success():
    """TavilyService.search 正常调用 TavilyClient 并返回 dict"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.return_value = {
        "results": [
            {"title": "美联储加息", "content": "美联储宣布加息25个基点...", "url": "https://example.com/1"}
        ]
    }

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        result = TavilyService.search(query="美联储利率", topic="news", max_results=5)

    assert "results" in result
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "美联储加息"
    mock_instance.search.assert_called_once_with(query="美联储利率", topic="news", max_results=5)


@pytest.mark.asyncio
async def test_tavily_service_search_empty_results():
    """TavilyService.search 返回空结果"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.return_value = {"results": []}

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        result = TavilyService.search(query="不存在的关键词", topic="news", max_results=5)

    assert result == {"results": []}


@pytest.mark.asyncio
async def test_tavily_service_search_api_error():
    """TavilyService.search API 异常时抛出"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.side_effect = Exception("API Key 无效")

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        with pytest.raises(Exception, match="API Key 无效"):
            TavilyService.search(query="测试", topic="news", max_results=5)
