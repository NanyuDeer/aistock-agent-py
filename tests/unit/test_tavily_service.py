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


@pytest.fixture(autouse=True)
def _clear_key_pool_cache():
    """模块级 _KEY_POOL_CACHE 是跨测试全局状态，逐测试清空保证 monkeypatch 隔离。"""
    from aistock_agent.services import tavily as tv_mod

    tv_mod._KEY_POOL_CACHE.clear()
    yield
    tv_mod._KEY_POOL_CACHE.clear()


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


def test_build_key_pools_reuses_same_instance_for_same_keys(monkeypatch):
    """同一 key 集合跨调用复用同一 KeyPool 实例（健康状态跨请求保持）"""
    from aistock_agent.services import tavily as tv_mod

    monkeypatch.setattr(settings, "tavily_api_key", "cache-key-a")
    monkeypatch.setattr(settings, "tavily_api_keys", "")

    pool1 = tv_mod._build_key_pools()["tavily"]
    pool2 = tv_mod._build_key_pools()["tavily"]
    assert pool1 is pool2


def test_build_key_pools_different_keys_yield_fresh_instance(monkeypatch):
    """不同 key 集合得到不同 KeyPool 实例（变更配置即换新池，保持 monkeypatch 隔离）"""
    from aistock_agent.services import tavily as tv_mod

    monkeypatch.setattr(settings, "tavily_api_key", "cache-key-a")
    monkeypatch.setattr(settings, "tavily_api_keys", "")
    pool1 = tv_mod._build_key_pools()["tavily"]

    monkeypatch.setattr(settings, "tavily_api_key", "cache-key-b")
    pool2 = tv_mod._build_key_pools()["tavily"]

    assert pool1 is not pool2


def test_key_pool_cooldown_persists_across_build_boundary(monkeypatch):
    """熔断冷却状态跨 _build_key_pools() 重建边界保持（单 key 全冷却 fail-open）"""
    from aistock_agent.services import tavily as tv_mod

    monkeypatch.setattr(settings, "tavily_api_key", "cooldown-key")
    monkeypatch.setattr(settings, "tavily_api_keys", "")

    pool = tv_mod._build_key_pools()["tavily"]
    pool.report_error("cooldown-key", is_circuit=True)

    pool_again = tv_mod._build_key_pools()["tavily"]
    assert pool_again is pool
    # 全冷却 fail-open：仍返回 key，且熔断开关打开（错误被记住）
    selected = pool_again.select_key()
    assert selected == "cooldown-key"
    assert pool_again.circuit_open is True


@pytest.mark.asyncio
async def test_tavily_service_search_missing_url_yields_empty(tavily_key):
    """结果无 url 时输出 url 为空字符串（不泄漏 '来源: None'）"""
    from aistock_agent.services.tavily import TavilyService

    mock_instance = MagicMock()
    mock_instance.search.return_value = {
        "results": [{"title": "t", "content": "c"}]
    }

    with patch("aistock_agent.services.tavily.TavilyClient", return_value=mock_instance):
        result = TavilyService.search(query="q", topic="news", max_results=5)

    assert result["results"][0]["url"] == ""
