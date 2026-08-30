"""L3 前瞻查询缓存（§4.8/D12）：当日去重 + 空结果负缓存 + 有界 + 过期清扫。"""
from aistock_agent.services.search_cache import SearchCache


def test_normalize_key_date_ified_query_not() -> None:
    k1 = SearchCache.normalize_key("2026-08-28", "下周 财经日历 重要事件 A股")
    k2 = SearchCache.normalize_key("2026-08-28", "下周 财经日历 重要事件 A股")
    k3 = SearchCache.normalize_key("2026-08-29", "下周 财经日历 重要事件 A股")
    assert k1 == k2
    assert k1 != k3
    assert k1.startswith("2026-08-28|")


def test_record_get_ok_dedup() -> None:
    cache = SearchCache()
    key = SearchCache.normalize_key("2026-08-28", "q")
    assert cache.get(key) is None
    cache.record(key, empty=False)
    state, _ = cache.get(key)
    assert state == "ok"


def test_empty_negative_cache_ttl() -> None:
    cache = SearchCache(empty_ttl_seconds=1)
    key = SearchCache.normalize_key("2026-08-28", "q")
    cache.record(key, empty=True)
    state, _ = cache.get(key)
    assert state == "empty"


def test_bounded_eviction() -> None:
    cache = SearchCache(max_entries=3)
    for i in range(5):
        cache.record(SearchCache.normalize_key("2026-08-28", f"q{i}"), empty=False)
    assert len(cache._store) <= 3
