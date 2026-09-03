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
    get_list.assert_awaited_once_with("/internal/predictions?status=pending&limit=200")
    assert rows == [{"id": 1}]


@pytest.mark.asyncio
async def test_list_pending_predictions_with_cursor(client: NodeApiClient):
    with patch.object(client, "get_list", new=AsyncMock(return_value=[{"id": 99}])) as get_list:
        rows = await client.list_pending_predictions(limit=50, before_id=100)
    get_list.assert_awaited_once_with("/internal/predictions?status=pending&limit=50&before_id=100")
    assert rows == [{"id": 99}]


@pytest.mark.asyncio
async def test_list_verified_predictions(client: NodeApiClient):
    with patch.object(client, "get_list", new=AsyncMock(return_value=[{"id": 5}])) as get_list:
        rows = await client.list_verified_predictions(limit=500)
    get_list.assert_awaited_once_with("/internal/predictions?status=verified&limit=500")
    assert rows == [{"id": 5}]


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
async def test_update_prediction_verification_condition_key_not_overridden_d5(
    client: NodeApiClient,
):
    """D5（2026-09-03）回归：condition 路径 key=c{i}、entry.horizon=anchor 档位，
    body 的 jsonb key 恒用 horizon 参数——entry 自带 horizon 字段不得覆盖 key，
    anchor 经 anchor_horizon 透传（供 Node 写 entry.horizon 统计分桶）。"""
    entry = {
        "horizon": "short",  # anchor 档位（与 jsonb key c{i} 不同）
        "result": "hit",
        "actual": "+1.2%",
        "reason": "窗口累计=+1.2%",
        "verified_at": "2026-09-03",
    }
    with patch.object(client, "put", new=AsyncMock(return_value={"id": 1})) as put:
        result = await client.update_prediction_verification(1, "c0", entry)
    put.assert_awaited_once_with(
        "/internal/predictions/1/verification",
        {"horizon": "c0", "anchor_horizon": "short", **{k: v for k, v in entry.items() if k != "horizon"}},
    )
    assert result == {"id": 1}


@pytest.mark.asyncio
async def test_get_index_kline_returns_rows(client: NodeApiClient):
    rows = [{"trade_date": "2026-08-11", "pct_chg": 1.2}]
    with patch.object(client, "get", new=AsyncMock(return_value={"rows": rows})) as get:
        result = await client.get_index_kline("000001", days=130)
    assert result == rows
    get.assert_awaited_once_with("/internal/index/000001/kline?days=130")


@pytest.mark.asyncio
async def test_get_index_kline_returns_none_on_failure(client: NodeApiClient):
    with patch.object(client, "get", new=AsyncMock(return_value=None)):
        assert await client.get_index_kline("000001", days=130) is None


@pytest.mark.asyncio
async def test_resolve_ths_name_matched(client: NodeApiClient):
    matched = {"matched": {"ts_code": "885525.TI", "name": "白酒概念"}}
    with patch.object(client, "get", new=AsyncMock(return_value=matched)):
        out = await client.resolve_ths_name("白酒板块")
    assert out == {"ts_code": "885525.TI", "name": "白酒概念"}


@pytest.mark.asyncio
async def test_resolve_ths_name_none(client: NodeApiClient):
    with patch.object(client, "get", new=AsyncMock(return_value={"matched": None})):
        assert await client.resolve_ths_name("不存在板块") is None


@pytest.mark.asyncio
async def test_get_ths_daily_range_parses_rows(client: NodeApiClient):
    resp = {"rows": [{"trade_date": "20250102", "pct_chg": 1.23}]}
    with patch.object(client, "get", new=AsyncMock(return_value=resp)):
        rows = await client.get_ths_daily_range("885525.TI", "20250101", "20251231")
    assert rows == [{"trade_date": "20250102", "pct_chg": 1.23}]


@pytest.mark.asyncio
async def test_get_index_kline_with_range_appends_params(client: NodeApiClient):
    captured = {}

    async def fake_get(path):
        captured["path"] = path
        return {"rows": []}

    with patch.object(client, "get", new=fake_get):
        await client.get_index_kline("000001", 200, start_date="20260101", end_date="20260131")
    assert "start_date=20260101" in captured["path"] and "end_date=20260131" in captured["path"]


@pytest.mark.asyncio
async def test_get_ths_index_map_returns_ts_codes(client: NodeApiClient):
    ts_codes = [{"name": "白酒概念", "ts_code": "885525.TI"}]
    with patch.object(client, "get", new=AsyncMock(return_value={"ts_codes": ts_codes})) as get:
        result = await client.get_ths_index_map()
    assert result == ts_codes
    get.assert_awaited_once_with("/internal/ths/index-map")


@pytest.mark.asyncio
async def test_get_ths_index_map_returns_none_on_failure(client: NodeApiClient):
    with patch.object(client, "get", new=AsyncMock(return_value=None)):
        assert await client.get_ths_index_map() is None
