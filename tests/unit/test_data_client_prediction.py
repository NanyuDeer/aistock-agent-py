from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import NodeApiClient


@pytest.fixture
def client() -> NodeApiClient:
    return NodeApiClient()


@pytest.mark.asyncio
async def test_save_prediction_posts_to_predictions(client: NodeApiClient):
    with patch.object(client, "post", new=AsyncMock(return_value={"id": 1})) as post:
        result = await client.save_prediction(
            {"source_type": "market_trace", "source_id": "review:2026-08-10"}
        )
    post.assert_awaited_once_with(
        "/internal/predictions",
        {"source_type": "market_trace", "source_id": "review:2026-08-10"},
    )
    assert result == {"id": 1}


@pytest.mark.asyncio
async def test_list_pending_predictions(client: NodeApiClient):
    with patch.object(client, "get_list", new=AsyncMock(return_value=[{"id": 1}])) as get_list:
        rows = await client.list_pending_predictions()
    get_list.assert_awaited_once_with("/internal/predictions?status=pending")
    assert rows == [{"id": 1}]


@pytest.mark.asyncio
async def test_update_prediction_verification_puts(client: NodeApiClient):
    entry = {
        "horizon": "mid",
        "result": "hit",
        "actual": "+1.2%",
        "reason": "方向一致",
        "verified_at": "2026-08-10",
    }
    with patch.object(client, "put", new=AsyncMock(return_value={"id": 1})) as put:
        result = await client.update_prediction_verification(1, "mid", entry)
    put.assert_awaited_once_with(
        "/internal/predictions/1/verification", {"horizon": "mid", **entry}
    )
    assert result == {"id": 1}


@pytest.mark.asyncio
async def test_get_index_kline_returns_rows(client: NodeApiClient):
    rows = [{"trade_date": "2026-08-11", "pct_chg": 1.2}]
    with patch.object(client, "get", new=AsyncMock(return_value={"code": 200, "data": {"rows": rows}})) as get:
        result = await client.get_index_kline("000001", days=130)
    assert result == rows
    get.assert_awaited_once_with("/internal/index/000001/kline?days=130")


@pytest.mark.asyncio
async def test_get_index_kline_returns_none_on_failure(client: NodeApiClient):
    with patch.object(client, "get", new=AsyncMock(return_value=None)):
        assert await client.get_index_kline("000001", days=130) is None
