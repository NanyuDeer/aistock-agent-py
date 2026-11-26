"""工具调用 memo：规范化参数、稳定 key 与并发在途合并。

同一 `(tool, 规范化参数)` 的并发调用只触达客户端一次，并共享同一结果；
失败不缓存（done 回调立即清理），调用方可重试。`evidence_id` 由 memo key
确定性派生，因此同参数调用必然得到同一证据 id。
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, TypeVar, cast

T = TypeVar("T")

#: 需要按股票代码口径归一的参数键。
_SYMBOL_KEYS: frozenset[str] = frozenset({"symbol", "subject_code", "ts_code"})


def normalize_symbol(value: str) -> str:
    """股票代码书写归一：去空白、大写、`.` 统一为 `-`。"""
    return value.strip().upper().replace(".", "-")


def _normalize_value(key: str, value: Any) -> Any:
    if isinstance(value, Mapping):
        return normalize_params(cast(Mapping[str, Any], value))
    if isinstance(value, list | tuple):
        return [_normalize_value(key, item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if key in _SYMBOL_KEYS:
            return normalize_symbol(text)
        return text
    return value


def normalize_params(params: Mapping[str, Any]) -> dict[str, Any]:
    """按键排序并归一参数，保证同义调用得到同一 memo key。"""
    normalized: dict[str, Any] = {}
    for key in sorted(params, key=str):
        normalized[str(key)] = _normalize_value(str(key), params[key])
    return normalized


def memo_key(tool: str, params: Mapping[str, Any]) -> str:
    """`<tool>:<sha256(规范化 JSON)>`，用于在途合并与稳定证据 id。"""
    payload = json.dumps(
        normalize_params(params),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{tool.strip().lower()}:{digest}"


def evidence_id_for(tool: str, params: Mapping[str, Any]) -> str:
    """evidence_id 由 memo key 确定性派生：同参数必然同一证据 id。"""
    digest = memo_key(tool, params).split(":", 1)[1]
    return f"ev_{digest[:24]}"


class Memo:
    """并发在途合并：同 key 只执行一次 factory。"""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Task[Any]] = {}
        self.hits = 0

    async def run(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.ensure_future(factory())
            self._inflight[key] = task

            def _on_done(done: asyncio.Task[Any]) -> None:
                self._discard(key, done)

            task.add_done_callback(_on_done)
        else:
            self.hits += 1
        return cast(T, await task)

    def _discard(self, key: str, task: asyncio.Task[Any]) -> None:
        # 仅清理仍是同一 task 的条目，避免误删后续重试创建的新 task。
        if self._inflight.get(key) is task:
            self._inflight.pop(key, None)
