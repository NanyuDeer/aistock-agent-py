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


def test_anysearch_provider_parses_real_api_double_wrapped():
    """真实 AnySearch API 返回 {code,message,data:{results:[...]}} 双层结构。

    旧实现只取顶层 results，把 data 字典当 list 遍历其键名（"results" 字符串）
    导致 hits 恒空 → 搜索链路静默空结果。回归测试锁定真实结构解析。
    """
    with patch("aistock_agent.services.search_providers.httpx.Client") as m:
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "code": 0,
            "message": "success",
            "request_id": "req-1",
            "data": {
                "results": [
                    {
                        "title": "澜起科技A股收跌17.6%",
                        "snippet": "港股盘中一度跌超20%，一天蒸发五百多",
                        "url": "https://xueqiu.com/S/SH688008",
                    },
                ]
            },
        }
        m.return_value.__enter__.return_value.post.return_value = resp
        prov = AnySearchProvider()
        r = prov.search("q", topic="news", max_results=5, api_key="k")
    assert len(r.hits) == 1
    assert r.hits[0].title == "澜起科技A股收跌17.6%"
    assert r.hits[0].content == "港股盘中一度跌超20%，一天蒸发五百多"
    assert r.hits[0].url == "https://xueqiu.com/S/SH688008"
    assert r.provider == "anysearch"
