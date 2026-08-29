"""data_client 节奏大师新增方法（正常 + 降级语义）。"""
from unittest.mock import AsyncMock

import pytest

from aistock_agent.services.data_client import NodeApiClient


@pytest.fixture
def client() -> NodeApiClient:
    return NodeApiClient()


def _mock_request(client: NodeApiClient, result: object) -> None:
    client._request = AsyncMock(return_value=result)  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_get_calendar_events_returns_events_list(client: NodeApiClient) -> None:
    _mock_request(client, {"events": [{"date": "2026-09-01", "title": "x"}]})
    events = await client.get_calendar_events("2026-09-01", "2026-09-05")
    assert events is not None and events[0]["title"] == "x"
    client._request.assert_called_once_with("/internal/calendar/events?dateFrom=2026-09-01&dateTo=2026-09-05")  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_get_calendar_events_non_dict_returns_none(client: NodeApiClient) -> None:
    _mock_request(client, None)
    assert await client.get_calendar_events("2026-09-01", "2026-09-05") is None


@pytest.mark.asyncio
async def test_post_calendar_event_passes_body(client: NodeApiClient) -> None:
    client._post_request = AsyncMock(return_value={"id": 1, "upserted": True})  # type: ignore[method-assign]
    body: dict[str, object] = {"event_date": "2026-09-01", "title": "测试", "source": "L3"}
    out = await client.post_calendar_event(body)
    assert out is not None and out["upserted"] is True


@pytest.mark.asyncio
async def test_get_fear_greed_ok(client: NodeApiClient) -> None:
    _mock_request(
        client,
        {"index": 62.5, "label": "贪婪", "indicators": [], "history": {"dates": [], "scores": []}},
    )
    out = await client.get_fear_greed()
    assert out is not None and out["index"] == 62.5


@pytest.mark.asyncio
async def test_get_fear_greed_caches_within_ttl(client: NodeApiClient) -> None:
    client._fear_greed_cache = {}  # 重置
    calls = 0

    async def fake_request(path: str) -> object:  # noqa: ANN001
        nonlocal calls
        calls += 1
        return {"index": 50.0, "label": "中性"}

    client._request = fake_request  # type: ignore[method-assign]
    await client.get_fear_greed()
    await client.get_fear_greed()
    assert calls == 1


@pytest.mark.asyncio
async def test_get_rhythm_report_slot(client: NodeApiClient) -> None:
    _mock_request(
        client, {"report_type": "rhythm_master", "content": {"refresh_slot": "after_close"}}
    )
    out = await client.get_rhythm_report("2026-09-01", "after_close")
    assert out is not None
    client._request.assert_called_once_with("/internal/analysis-reports/rhythm_master/2026-09-01/after_close")  # type: ignore[attr-defined]
