"""KeyPool — 单供应商多 key 的健康感知选择池。

辩论（2026-08-18）裁决：替换 config.get_tavily_key 的随机 random.choice，
提供"健康集合内随机 + 熔断冷却 + 全冷却 fail-open + 退避封顶"。
429/401 是限流信号（不指数退避，固定窗口）；5xx/网络错是熔断信号（指数退避）。
"""

import random
import time
from collections.abc import Sequence


class KeyPool:
    def __init__(
        self,
        keys: Sequence[str],
        *,
        cooldown_base_seconds: float = 5.0,
        max_backoff_seconds: float = 60.0,
    ) -> None:
        self._keys: list[str] = [k for k in keys if k]
        self._cooldown_base = cooldown_base_seconds
        self._max_backoff = max_backoff_seconds
        self._fail_streak: dict[str, int] = {k: 0 for k in self._keys}
        self._ready_at: dict[str, float] = {k: 0.0 for k in self._keys}
        self._last_seen_error: dict[str, float] = {k: 0.0 for k in self._keys}
        self.circuit_open = False

    def _cooldown_until(self, key: str) -> float:
        return self._ready_at[key]

    def select_key(self) -> str:
        now = time.monotonic()
        healthy = [k for k in self._keys if self._ready_at[k] <= now]
        if healthy:
            self.circuit_open = False
            return random.choice(healthy)
        # 全冷却 fail-open：选距上次失败最久（最接近恢复）的 key；打开熔断开关。
        self.circuit_open = True
        return max(self._keys, key=lambda k: self._last_seen_error[k])

    def report_success(self, key: str) -> None:
        self._fail_streak[key] = 0
        self._ready_at[key] = 0.0
        self.circuit_open = False

    def report_error(self, key: str, *, is_circuit: bool) -> None:
        self._last_seen_error[key] = time.monotonic()
        if is_circuit:
            self._fail_streak[key] += 1
            attempts = self._fail_streak[key]
            backoff = self._cooldown_base * (2 ** max(attempts - 1, 0))
            backoff = min(backoff, self._max_backoff)
            self._ready_at[key] = time.monotonic() + backoff
        else:
            # 限流/鉴权信号：固定窗口，不指数累积
            self._ready_at[key] = time.monotonic() + self._cooldown_base
