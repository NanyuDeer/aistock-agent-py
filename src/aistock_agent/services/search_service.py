"""SearchService 契约与请求级 failover 编排。

辩论（2026-08-18）裁决：编排层只做 failover 与低质闸门，不抛异常；
对外异常语义由 TavilyService.search 决定。provider 值域仅在此封顶。
"""

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

from aistock_agent.services.key_pool import KeyPool

ProviderName = Literal["tavily", "doubao", "anysearch"]
_MIN_AVG_CHARS = 50


class RateLimited(Exception):  # noqa: N818 — 名称由 search 契约固定（B1 修订），不采用 Error 后缀
    """429/401：限流或鉴权失败，触发固定窗口冷却而非指数退避。

    单一公共异常类（评审 B1 修订）：provider（search_providers/tavily）抛它，
    编排层 `search_query` 用 isinstance 识别限流。禁止各模块另立同名类。
    """


@dataclass(frozen=True)
class _Hit:
    title: str
    content: str
    url: str | None = None


@dataclass(frozen=True)
class SearchResult:
    provider: ProviderName
    hits: list[_Hit]
    outcome: Literal["ok", "degraded", "empty", "error"]
    provider_errors: list[tuple[str, str]] = field(default_factory=list)


class SearchProvider(Protocol):
    name: ProviderName

    def search(
        self, query: str, *, topic: str, max_results: int, api_key: str
    ) -> SearchResult:
        ...


class Budget:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline

    def remaining(self) -> float:
        return self.deadline - time.monotonic()

    def expired(self) -> bool:
        return time.monotonic() >= self.deadline


def is_low_quality(result: SearchResult, *, min_avg_chars: int = _MIN_AVG_CHARS) -> bool:
    if not result.hits:
        return True
    lengths = [
        len(h.content.strip()) for h in result.hits if h.content and h.content.strip()
    ]
    if not lengths:
        return True
    if any(not h.url for h in result.hits):
        return True
    return sum(lengths) / len(lengths) < min_avg_chars


def search_query(
    query: str,
    *,
    providers: Sequence[SearchProvider],
    keys: dict[str, KeyPool],
    budget_seconds: float = 10.0,
    topic: str = "news",
    max_results: int = 5,
) -> SearchResult:
    budget = Budget(time.monotonic() + budget_seconds)
    errors: list[tuple[str, str]] = []
    first_provider = providers[0].name if providers else "tavily"
    for provider in providers:
        if budget.expired():
            errors.append((provider.name, "budget_exhausted"))
            break
        key_pool = keys.get(provider.name)
        if key_pool is None or not key_pool._keys:
            errors.append((provider.name, "no_keys_configured"))
            continue
        api_key = key_pool.select_key()
        try:
            result = provider.search(
                query, topic=topic, max_results=max_results, api_key=api_key
            )
            if result.outcome == "error":
                errors.append((provider.name, "provider_error"))
                key_pool.report_error(api_key, is_circuit=True)
                continue
            key_pool.report_success(api_key)
            if provider.name != first_provider:
                degraded = is_low_quality(result)
                return SearchResult(
                    provider=result.provider,
                    hits=result.hits,
                    outcome="degraded" if degraded else result.outcome,
                    provider_errors=errors,
                )
            return result
        except Exception as exc:  # noqa: BLE001 — 网络/API 异常都算熔断
            is_quota = isinstance(exc, RateLimited)
            errors.append((provider.name, type(exc).__name__))
            key_pool.report_error(api_key, is_circuit=not is_quota)
    return SearchResult(
        provider=first_provider,
        hits=[],
        outcome="error",
        provider_errors=errors,
    )
