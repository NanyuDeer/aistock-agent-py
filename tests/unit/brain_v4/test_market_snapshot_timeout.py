"""行情快照接口必须使用更长的专用超时。

实测（2026-11-27 服务器）：`close-snapshot` 10.33s、`last-close-snapshot` 10.86s，
而 `HttpClientPool` 默认超时 10s，导致 V4 把“数据已就绪”稳定误判为缺失。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import HttpClientPool, NodeApiClient


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return {"code": 200, "data": {"trade_date": "20261127"}}


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    async def get(self, url: str, **kwargs: object) -> _Response:
        self.calls.append((url, dict(kwargs)))
        return _Response()


@pytest.mark.asyncio
async def test_close_snapshot_uses_market_snapshot_timeout() -> None:
    client = NodeApiClient()
    fake = _FakeClient()
    with patch.object(HttpClientPool, "get_client", AsyncMock(return_value=fake)):
        data = await client.get_close_snapshot("20261127")
        last = await client.get_last_close_snapshot()

    assert data == {"trade_date": "20261127"}
    assert last == {"trade_date": "20261127"}
    assert [call[1]["timeout"] for call in fake.calls] == [
        NodeApiClient._MARKET_SNAPSHOT_TIMEOUT_S,
        NodeApiClient._MARKET_SNAPSHOT_TIMEOUT_S,
    ]


@pytest.mark.asyncio
async def test_generic_get_keeps_default_timeout() -> None:
    client = NodeApiClient()
    fake = _FakeClient()
    with patch.object(HttpClientPool, "get_client", AsyncMock(return_value=fake)):
        await client.get("/internal/health")

    assert "timeout" not in fake.calls[0][1]
