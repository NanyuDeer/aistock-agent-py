"""L3 前瞻查询缓存（spec §4.8/D12）。

缓存三分原则落地：
- 跨日解析实体 → 落盘 market_calendar_events，不落搜索结果快照；
- 本模块只做「同日同语义 query 去重付费」与「空结果负缓存」两类场景；
- 禁止周级成功快照缓存（会把周内新预告冻结在周一快照里）。

注意：不可仿 report_cache.py（无界普通 dict 只增不清）——本模块必须有界 + 过期清扫。
"""
from __future__ import annotations

import hashlib
import threading
import time

State = tuple[str, float]  # ("ok" | "empty", timestamp)


class SearchCache:
    """进程内有界缓存。threading.Lock 保证 AsyncIOScheduler 多任务并发安全。"""

    def __init__(
        self,
        *,
        max_entries: int = 200,
        empty_ttl_seconds: int = 7200,
        day_ttl_seconds: int = 86400,
    ) -> None:
        self._store: dict[str, State] = {}
        self.max_entries = max_entries
        self.empty_ttl_seconds = empty_ttl_seconds
        self.day_ttl_seconds = day_ttl_seconds
        self._lock = threading.Lock()

    @staticmethod
    def normalize_key(basis_date: str, query: str) -> str:
        """key 日期化（§4.8 H5）：basis_date + query hash；query 文本保持自然语言不日期化。"""
        digest = hashlib.sha1(query.strip().lower().encode("utf-8")).hexdigest()[:12]
        return f"{basis_date}|{digest}"

    def get(self, key: str) -> tuple[str, bool] | None:
        """返回 (state, fresh)；None = 未命中（允许查询）。过期自动清除。"""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            state, ts = entry
            ttl = self.empty_ttl_seconds if state == "empty" else self.day_ttl_seconds
            if time.time() - ts > ttl:
                self._store.pop(key, None)
                return None
            return state, True

    def record(self, key: str, *, empty: bool) -> None:
        with self._lock:
            self._store[key] = ("empty" if empty else "ok", time.time())
            if len(self._store) > self.max_entries:
                self._evict_oldest()

    def cleanup(self) -> None:
        """惰性过期清扫（按需调用，周期可 30min）。"""
        now = time.time()
        with self._lock:
            expired = [
                key
                for key, (state, ts) in self._store.items()
                if now - ts > (self.empty_ttl_seconds if state == "empty" else self.day_ttl_seconds)
            ]
            for key in expired:
                self._store.pop(key, None)

    def _evict_oldest(self) -> None:
        if not self._store:
            return
        oldest_key = min(self._store, key=lambda k: self._store[k][1])
        self._store.pop(oldest_key, None)
