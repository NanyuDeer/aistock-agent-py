"""统一工具网关：allowlist、超时、时间边界与 provenance 信封。

硬约束：
- `as_of` 必须由调用方显式传入，网关绝不读取系统时间当 `as_of`；
- `fetched_at` 仅记录抓取时刻，可用注入 clock，默认 `datetime.now(timezone.utc)`
  绝不用于 `as_of`；
- `published_at > as_of` 的数据一律返回 `error="future_data"` 且 `data is None`。
"""

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from .memo import Memo, evidence_id_for, memo_key, normalize_params
from .provenance import GatewayError, QualityLevel, ToolEnvelope

#: 默认工具白名单（覆盖 profile 声明过的工具组 + 证据适配器用到的原子工具）。
DEFAULT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "news",
        "announcement",
        "research",
        "calendar",
        "crawl",
        "quote",
        "realtime_quote",
        "kline",
        "market_snapshot",
        "event_evidence",
        "trend",
        "fundamental",
        "verification",
    }
)


@dataclass(frozen=True)
class ToolClientResult:
    """客户端返回的原始结果：数据 + 三时间中最关键的两个 + 质量。"""

    data: Any
    published_at: datetime | None = None
    event_time: datetime | None = None
    quality: QualityLevel = "medium"


class ToolClient(Protocol):
    """工具客户端协议：网关只依赖这一层，便于注入 fake。"""

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult: ...


def _aware(value: datetime) -> datetime:
    """统一时区语义：naive 时间按 UTC 解释，避免 aware/naive 比较报错。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def parse_datetime(value: str | datetime) -> datetime:
    """解析显式时间边界；字符串允许 `Z` 结尾。"""
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


class ToolGateway:
    """所有证据取数的唯一入口。"""

    def __init__(
        self,
        client: ToolClient,
        *,
        as_of: str | datetime,
        allowed_tools: Iterable[str] | None = None,
        timeout_s: float = 10.0,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client = client
        self._as_of = parse_datetime(as_of)
        self._allowed = (
            frozenset(allowed_tools) if allowed_tools is not None else DEFAULT_ALLOWED_TOOLS
        )
        self._timeout_s = timeout_s
        self._clock = clock or (lambda: datetime.now(UTC))
        self._memo = Memo()
        self.trace: list[dict[str, Any]] = []
        self.counters: dict[str, int] = {"calls": 0, "memo_hits": 0, "errors": 0}

    @property
    def as_of(self) -> datetime:
        return self._as_of

    async def call(self, tool: str, params: dict[str, Any]) -> ToolEnvelope:
        normalized_tool = tool.strip().lower()
        normalized_params = normalize_params(params)
        evidence_id = evidence_id_for(normalized_tool, normalized_params)

        if normalized_tool not in self._allowed:
            return self._error(normalized_tool, normalized_params, evidence_id, "not_allowed")

        key = memo_key(normalized_tool, normalized_params)
        try:
            result = await asyncio.wait_for(
                self._memo.run(key, lambda: self._client.fetch(normalized_tool, normalized_params)),
                timeout=self._timeout_s,
            )
        except TimeoutError:
            return self._error(
                normalized_tool,
                normalized_params,
                evidence_id,
                "timeout",
                detail=f"tool {normalized_tool} exceeded {self._timeout_s}s",
            )
        except Exception as exc:  # noqa: BLE001 - 上游异常统一分类为 upstream_error
            return self._error(
                normalized_tool,
                normalized_params,
                evidence_id,
                "upstream_error",
                detail=str(exc),
            )

        if result.published_at is not None and _aware(result.published_at) > _aware(self._as_of):
            return self._error(
                normalized_tool,
                normalized_params,
                evidence_id,
                "future_data",
                detail="published_at is after as_of",
            )

        return self._envelope(
            normalized_tool,
            normalized_params,
            evidence_id,
            result=result,
        )

    def _error(
        self,
        tool: str,
        params: dict[str, Any],
        evidence_id: str,
        error: GatewayError,
        *,
        detail: str | None = None,
    ) -> ToolEnvelope:
        return self._envelope(tool, params, evidence_id, error=error, detail=detail)

    def _envelope(
        self,
        tool: str,
        params: dict[str, Any],
        evidence_id: str,
        *,
        result: ToolClientResult | None = None,
        error: GatewayError | None = None,
        detail: str | None = None,
    ) -> ToolEnvelope:
        envelope = ToolEnvelope(
            tool=tool,
            params=params,
            evidence_id=evidence_id,
            event_time=result.event_time if result is not None else None,
            published_at=result.published_at if result is not None else None,
            fetched_at=_aware(self._clock()),
            as_of=self._as_of,
            quality=result.quality if result is not None else "medium",
            error=error,
            error_detail=detail,
            data=result.data if result is not None else None,
        )
        self.counters["calls"] += 1
        self.counters["memo_hits"] = self._memo.hits
        if error is not None:
            self.counters["errors"] += 1
            self.counters[f"error:{error}"] = self.counters.get(f"error:{error}", 0) + 1
        self.trace.append({"tool": tool, "evidence_id": evidence_id, "error": error})
        return envelope
