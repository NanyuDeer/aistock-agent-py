"""非 Tavily 的搜索引擎 Provider — 把外部 API 结果归一到 SearchResult/_Hit。

Doubao 走火山引擎 search-infinity Global 版 /search_api/global_search（Bearer），
AnySearch 走 https://api.anysearch.com/v1/search（Bearer，每日 1000 次、finance 垂直域）。
429/401 抛 RateLimited（触发固定窗口冷却）。Tavily 的 provider 在
services/tavily.py 内（需保住 conftest 对 TavilyClient 的 patch 目标）。
"""

import httpx

from aistock_agent.services.search_service import ProviderName, RateLimited, SearchResult, _Hit

_ANYSEARCH_ENDPOINT = "https://api.anysearch.com/v1/search"


class AnySearchProvider:
    """AnySearch（anysearch.com）— 每日 1000 次日更、中文友好、finance 垂直域。

    走 `POST /v1/search`，鉴权 `Authorization: Bearer <key>`（API 本身支持无 key
    匿名调用、限流更低；但链路惰性注册要求配置 ANYSEARCH_API_KEY(S) 才会入链，
    故匿名模式在链上不可达）。body: `{query, max_results, domain:"finance",
    content_types:["web","news"], zone:"cn"}`（finance 垂直域 + cn 区，适配 A 股）。
    返回每结果含 snippet/content。429/401 → RateLimited；其余非 2xx → raise。
    """

    name: ProviderName = "anysearch"

    def search(
        self, query: str, *, topic: str, max_results: int, api_key: str
    ) -> SearchResult:
        headers = {
            "Authorization": f"Bearer {api_key}" if api_key else "",
            "Content-Type": "application/json",
        }
        payload = {
            "query": query,
            "max_results": max_results,
            "domain": "finance",
            "content_types": ["web", "news"],
            "zone": "cn",
        }
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(_ANYSEARCH_ENDPOINT, headers=headers, json=payload)
        if resp.status_code in (401, 429):
            raise RateLimited(f"{resp.status_code}: {resp.text[:80]}")
        resp.raise_for_status()
        data = resp.json()
        # 真实 API 返回 {code,message,data:{results:[...]}} 双层结构；旧实现只取
        # 顶层 results 后把 data 字典当 list 遍历键名，导致 hits 恒空（2026-08-28
        # 服务器实测：anysearch 搜索全空、触发熔断、快照新闻证据缺失）。
        # 兼容两层：顶层 results 优先，否则取 data.results。
        items = data.get("results") if isinstance(data, dict) else None
        if not isinstance(items, list) and isinstance(data, dict):
            inner = data.get("data")
            items = inner.get("results") if isinstance(inner, dict) else None
        hits: list[_Hit] = []
        for it in (items or []):
            if not isinstance(it, dict):
                continue
            hits.append(
                _Hit(
                    title=str(it.get("title") or it.get("Title") or "无标题"),
                    content=str(it.get("snippet") or it.get("content") or ""),
                    url=it.get("url") or it.get("Url") or it.get("link") or None,
                )
            )
        return SearchResult(
            provider="anysearch",
            hits=hits,
            outcome="empty" if not hits else "ok",
        )


class DoubaoProvider:
    """豆包搜索（火山引擎 search-infinity）— 中文/全球搜索兜底。

    走 Global 版 `POST /search_api/global_search`，响应字段已核准：
    `Result.Documents[].Title/Url/Snippet[].Text`。Custom 版 `web_search`
    响应字段未在当前文档核对，留作后续切换点。免费额度=火山账号每月 500 次
    （每月 1 日重置、优先消耗），另有订阅套餐（轻量 1000 次/月、每日限 50 次）。
    429/401 → RateLimited；接口层 Error → RuntimeError。
    """

    name: ProviderName = "doubao"
    _ENDPOINT = "https://open.feedcoopapi.com/search_api/global_search"
    _AUTH = "Bearer"

    def search(
        self, query: str, *, topic: str, max_results: int, api_key: str
    ) -> SearchResult:
        headers = {"Authorization": f"{self._AUTH} {api_key}", "Content-Type": "application/json"}
        payload = {"Query": query, "DocCount": max(max_results, 1), "MaxSnippetLength": 500}
        with httpx.Client(timeout=8.0) as client:
            resp = client.post(self._ENDPOINT, headers=headers, json=payload)
        if resp.status_code in (401, 429):
            raise RateLimited(f"{resp.status_code}: {resp.text[:80]}")
        resp.raise_for_status()
        data = resp.json()
        meta = data.get("ResponseMetadata") if isinstance(data, dict) else None
        if isinstance(meta, dict) and meta.get("Error"):
            raise RuntimeError(f"doubao error: {meta['Error']}")
        result_obj = data.get("Result") if isinstance(data, dict) else None
        docs = result_obj.get("Documents") if isinstance(result_obj, dict) else []
        hits: list[_Hit] = []
        for d in (docs or []):
            if not isinstance(d, dict):
                continue
            snippets = d.get("Snippet") or []
            text = ""
            for s in snippets:
                if isinstance(s, dict) and s.get("Type") == "text":
                    text = str(s.get("Text") or "")
                    break
            hits.append(
                _Hit(title=str(d.get("Title", "无标题")), content=text, url=d.get("Url") or None)
            )
        return SearchResult(
            provider="doubao",
            hits=hits,
            outcome="empty" if not hits else "ok",
        )
