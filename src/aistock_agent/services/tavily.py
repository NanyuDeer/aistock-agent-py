"""Tavily 客户端封装层

将 Tavily API 调用从 tools/market_tools.py 抽出，
形成独立 service 层，供 tools/search_tools.py 调用。

多供应商 failover（辩论 2026-08-18）：TavilyService.search 走统一编排
search_query，按 tavily→doubao→anysearch 顺序失败切换；返回 dict 追加
加性键 provider（真实命中源）与成功时 outcome（ok/degraded/empty）。
"""

import logging

from tavily import TavilyClient  # type: ignore[import-untyped]

from aistock_agent.config import settings
from aistock_agent.services.key_pool import KeyPool
from aistock_agent.services.search_providers import AnySearchProvider, DoubaoProvider
from aistock_agent.services.search_service import (
    ProviderName,
    RateLimited,
    SearchProvider,
    SearchResult,
    _Hit,
)

logger = logging.getLogger(__name__)

# 模块级 KeyPool 单例缓存：按 (provider_name, key_tuple) 复用同一实例，
# 使冷却/失败计数/circuit_open 健康状态跨请求保持（评审 Finding #1）。
# 缓存规模受进程内不同 key 集合数量约束（有界），key 集合变化时自动换新池，
# 也因此天然支持测试 monkeypatch 隔离（不同 key → 不同缓存键）。
_KEY_POOL_CACHE: dict[tuple[str, tuple[str, ...]], KeyPool] = {}


class TavilyClientProvider:
    """Tavily 主源 Provider —— 必须留在 tavily.py，复用模块级 TavilyClient，
    保住 conftest/e2e 等既有 patch("aistock_agent.services.tavily.TavilyClient")。
    """

    name: ProviderName = "tavily"

    def search(
        self, query: str, *, topic: str, max_results: int, api_key: str
    ) -> SearchResult:
        try:
            raw = TavilyClient(api_key=api_key).search(
                query=query, topic=topic, max_results=max_results
            )
        except Exception as exc:  # noqa: BLE001
            status = getattr(exc, "status_code", None)
            if status in (401, 429):
                raise RateLimited(str(exc)) from exc
            raise
        items = raw.get("results") if isinstance(raw, dict) else []
        hits: list[_Hit] = []
        for item in (items or []):
            if isinstance(item, dict):
                hits.append(
                    _Hit(
                        title=str(item.get("title", "无标题")),
                        content=str(item.get("content", "")),
                        url=item.get("url") or None,
                    )
                )
        return SearchResult(
            provider="tavily",
            hits=hits,
            outcome="empty" if not hits else "ok",
        )


class TavilyService:
    """Tavily 搜索服务封装"""

    @staticmethod
    def search(query: str, *, topic: str = "news", max_results: int = 5) -> dict[str, object]:
        """统一搜索入口，含多供应商 failover（辩论 2026-08-18）。

        返回 dict 追加加性键 ``provider``（真实命中源）与成功时 ``outcome``；
        只读 title/content/url 的消费端零破坏。全部失败抛异常→上层降级。

        Args:
            query: 搜索关键词
            topic: 搜索类型（news / general）
            max_results: 最大结果数

        Returns:
            dict，含 ``results`` 及加性 ``provider``/``outcome`` 键

        Raises:
            RuntimeError: 所有 provider 均失败时抛出（由上层 @safe_tool_call 降级处理）
        """
        from aistock_agent.services.search_service import search_query

        providers = _build_providers()
        keys = _build_key_pools()
        result = search_query(
            query,
            providers=providers,
            keys=keys,
            budget_seconds=settings.search_budget_seconds,
            topic=topic,
            max_results=max_results,
        )
        if result.outcome == "error":
            raise RuntimeError(
                f"all search providers failed: {result.provider_errors}"
            )
        # 评审 N 修订：任一 key 池全冷却 fail-open → 记 HIGH 告警（观测性承诺落地）
        for pool in keys.values():
            if pool.circuit_open:
                logger.warning(
                    "search_provider_circuit_open provider=%s errors=%s",
                    result.provider,
                    result.provider_errors,
                )
                break
        out: dict[str, object] = {
            "results": [
                {"title": h.title, "content": h.content, "url": h.url or ""}
                for h in result.hits
            ],
            "provider": result.provider,
            "outcome": result.outcome,
        }
        return out


def _build_providers() -> list[SearchProvider]:
    enabled = {
        p.strip() for p in (settings.search_enabled_providers or "").split(",") if p.strip()
    } or {"tavily", "doubao", "anysearch"}
    chain: list[SearchProvider] = []
    if "tavily" in enabled and (settings.tavily_api_key or settings.tavily_api_keys):
        chain.append(TavilyClientProvider())
    if "doubao" in enabled and (settings.doubao_api_key or settings.doubao_api_keys):
        chain.append(DoubaoProvider())
    if "anysearch" in enabled and (settings.anysearch_api_key or settings.anysearch_api_keys):
        chain.append(AnySearchProvider())
    if not chain:
        chain.append(TavilyClientProvider())  # 配置缺失时保底主源
    return chain


def _build_key_pools() -> dict[str, KeyPool]:
    """构建/复用模块级 KeyPool 缓存。

    KeyPool.__init__ 已过滤空串，get_*_keys 已 strip/过滤空值，
    因此缓存键 tuple 天然干净。
    """
    pools: dict[str, KeyPool] = {}
    for name, keys in (
        ("tavily", settings.get_tavily_keys()),
        ("doubao", settings.get_doubao_keys()),
        ("anysearch", settings.get_anysearch_keys()),
    ):
        if not keys:
            continue
        cache_key = (name, tuple(keys))
        pool = _KEY_POOL_CACHE.get(cache_key)
        if pool is None:
            pool = KeyPool(list(keys))
            _KEY_POOL_CACHE[cache_key] = pool
        pools[name] = pool
    return pools
