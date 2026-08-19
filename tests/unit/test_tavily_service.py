"""Tavily 客户端封装层测试 — mock TavilyClient。

评审 B3 修订：新实现经 failover chain 走 _build_key_pools()，
三用例显式注入 key（settings.tavily_api_key），否则 chain 缺 key → RuntimeError。
"""

from unittest.mock import MagicMock, patch

import pytest

from aistock_agent.config import settings


@pytest.fixture
def tavily_key(monkeypatch):
    monkeypatch.setattr(settings, "tavily_api_key", "test-key")
    return "test-key"


@pytest.mark.asyncio
async def test_tavily_service_search_success(tavily_key):
    """TavilyService.search 正常调用 TavilyClient 并返回 dict（加性 provider 键）"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.return_value = {
        "results": [
            {"title": "美联储加息", "content": "美联储宣布加息25个基点", "url": "https://example.com/1"}
        ]
    }

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        result = TavilyService.search(query="美联储利率", topic="news", max_results=5)

    assert "results" in result
    assert len(result["results"]) == 1
    assert result["results"][0]["title"] == "美联储加息"
    assert result["provider"] == "tavily"       # 加性键
    assert result["outcome"] in {"ok", "empty", "degraded"}
    mock_instance.search.assert_called_once_with(query="美联储利率", topic="news", max_results=5)


@pytest.mark.asyncio
async def test_tavily_service_search_empty_results(tavily_key):
    """TavilyService.search 返回空结果（带 provider/outcome 加性键）"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.return_value = {"results": []}

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        result = TavilyService.search(query="不存在的关键词", topic="news", max_results=5)

    assert result == {"results": [], "provider": "tavily", "outcome": "empty"}


@pytest.mark.asyncio
async def test_tavily_service_search_api_error(tavily_key):
    """TavilyService.search API 异常 → failover 全失败 → 抛 RuntimeError（调用方降级）"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.side_effect = Exception("API Key 无效")

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        with pytest.raises(RuntimeError, match="all search providers failed"):
            TavilyService.search(query="测试", topic="news", max_results=5)


def test_tavily_service_passes_provider_key(monkeypatch):
    """failover 编排返回的真实命中源落到加性 provider 键
    （逐 provider 切换已在 search_query 单测覆盖）
    """
    from unittest.mock import patch as _patch

    import aistock_agent.services.tavily as tv_mod
    from aistock_agent.services.search_service import SearchResult

    with _patch("aistock_agent.services.search_service.search_query") as mock_query:
        mock_query.return_value = SearchResult(
            provider="anysearch", hits=[], outcome="ok", provider_errors=[]
        )
        result = tv_mod.TavilyService.search("美联储利率")
    assert result["provider"] == "anysearch"
    assert result["outcome"] == "ok"
    assert "results" in result
