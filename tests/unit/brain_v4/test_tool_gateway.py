"""统一证据网关测试：时间边界、memo 并发去重与错误分类。

覆盖（Task 3 Step 1-2）：
- 发布时间晚于 as_of 的数据必须被拒绝，data 为 None；
- 同参数并发调用只触达客户端一次，并返回同一 evidence_id；
- 未知工具 not_allowed、超时 timeout、上游异常 upstream_error；
- fetched_at 只来自注入时钟，不读系统时间。
"""

import asyncio
from datetime import datetime
from typing import Any

import pytest

from aistock_agent.brain_v4.tools.gateway import ToolClientResult, ToolGateway

pytestmark = pytest.mark.asyncio

AS_OF = "2026-11-26T10:00:00+08:00"
FIXED_NOW = datetime.fromisoformat("2026-11-26T10:00:05+08:00")


def _dt(text: str) -> datetime:
    return datetime.fromisoformat(text)


def _result(
    *,
    published_at: str | None = "2026-11-26T09:00:00+08:00",
    event_time: str | None = "2026-11-26T08:59:00+08:00",
) -> ToolClientResult:
    return ToolClientResult(
        data={"ok": True},
        published_at=_dt(published_at) if published_at else None,
        event_time=_dt(event_time) if event_time else None,
    )


class FakeClient:
    """记录调用次数与参数的最小客户端，可注入延迟或异常。"""

    def __init__(
        self,
        result: ToolClientResult | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0.0,
    ) -> None:
        self.result = result if result is not None else _result()
        self.error = error
        self.delay = delay
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult:
        self.calls.append((tool, dict(params)))
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.result


async def test_gateway_rejects_published_after_as_of() -> None:
    client = FakeClient(
        _result(
            published_at="2026-11-26T11:00:00+08:00",
            event_time="2026-11-26T10:59:00+08:00",
        )
    )
    gateway = ToolGateway(client, as_of=AS_OF, clock=lambda: FIXED_NOW)

    result = await gateway.call("news", {"symbol": "600519-SH"})

    assert result.error == "future_data"
    assert result.data is None


async def test_memo_deduplicates_concurrent_identical_calls() -> None:
    client = FakeClient(delay=0.05)
    gateway = ToolGateway(client, as_of=AS_OF, clock=lambda: FIXED_NOW)

    first, second = await asyncio.gather(
        gateway.call("news", {"symbol": "600519.SH", "limit": 5}),
        gateway.call("news", {"limit": 5, "symbol": "600519-SH"}),
    )

    assert len(client.calls) == 1
    assert first.evidence_id == second.evidence_id
    assert first.error is None
    assert second.error is None


async def test_memo_failure_is_not_cached_and_can_retry() -> None:
    client = FakeClient(error=RuntimeError("upstream down"))
    gateway = ToolGateway(client, as_of=AS_OF, clock=lambda: FIXED_NOW)

    first = await gateway.call("news", {"symbol": "600519-SH"})
    assert first.error == "upstream_error"

    client.error = None
    second = await gateway.call("news", {"symbol": "600519-SH"})
    assert second.error is None
    assert len(client.calls) == 2


async def test_unknown_tool_is_rejected_without_calling_client() -> None:
    client = FakeClient()
    gateway = ToolGateway(client, as_of=AS_OF, clock=lambda: FIXED_NOW)

    result = await gateway.call("shell_exec", {})

    assert result.error == "not_allowed"
    assert result.data is None
    assert client.calls == []


async def test_timeout_is_classified() -> None:
    client = FakeClient(delay=0.2)
    gateway = ToolGateway(
        client,
        as_of=AS_OF,
        timeout_s=0.01,
        allowed_tools=frozenset({"news"}),
        clock=lambda: FIXED_NOW,
    )

    result = await gateway.call("news", {"symbol": "600519-SH"})

    assert result.error == "timeout"
    assert result.data is None


async def test_success_envelope_uses_injected_clock_and_explicit_as_of() -> None:
    client = FakeClient()
    gateway = ToolGateway(client, as_of=AS_OF, clock=lambda: FIXED_NOW)

    result = await gateway.call("news", {"symbol": "600519"})

    assert result.error is None
    assert result.data == {"ok": True}
    assert result.as_of == _dt(AS_OF)
    assert result.fetched_at == FIXED_NOW
    assert result.evidence_id.startswith("ev_")
    assert result.tool == "news"
    assert result.published_at == _dt("2026-11-26T09:00:00+08:00")
