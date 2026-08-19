"""非 Tavily 搜索引擎 Provider 测试 — mock httpx.Client（只测 Doubao/AnySearch）。

这些 provider 走 httpx，不依赖 TavilyClient，因此与 tavily 无关。
"""

from unittest.mock import MagicMock, patch

from aistock_agent.services.search_providers import AnySearchProvider, DoubaoProvider


def test_doubao_provider_parses_global_documents():
    with patch("aistock_agent.services.search_providers.httpx.Client") as m:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "ResponseMetadata": {"RequestId": "x"},
            "Result": {
                "Documents": [
                    {
                        "Title": "豆包头条",
                        "Url": "https://doubao.example/a",
                        "Snippet": [{"Type": "text", "Text": "一段摘要文本"}],
                    },
                ]
            },
        }
        m.return_value.__enter__.return_value.post.return_value = resp
        prov = DoubaoProvider()
        r = prov.search("q", topic="news", max_results=5, api_key="k")
    assert r.hits[0].title == "豆包头条"
    assert r.hits[0].content == "一段摘要文本"
    assert r.hits[0].url == "https://doubao.example/a"
    assert r.provider == "doubao"


def test_anysearch_provider_parses_results():
    with patch("aistock_agent.services.search_providers.httpx.Client") as m:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "results": [
                {"title": "A股头条", "snippet": "一段结构化摘要", "url": "http://as.example/a"},
            ]
        }
        m.return_value.__enter__.return_value.post.return_value = resp
        prov = AnySearchProvider()
        r = prov.search("q", topic="news", max_results=5, api_key="k")
    assert r.hits[0].title == "A股头条"
    assert r.hits[0].content == "一段结构化摘要"
    assert r.hits[0].url == "http://as.example/a"
    assert r.provider == "anysearch"