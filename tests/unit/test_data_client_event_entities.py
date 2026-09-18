"""data_client Event Entity 封装单元测试（spec §10.2）。

覆盖：
- post_event_entity：POST /internal/event-entities（app-api 权威 event_id 生成）
- 失败/端点未落地 → 返回 None（调用方降级跳过，不抛异常）
- get_event_entities：GET /internal/event-entities（status/日期过滤）
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import node_api


@pytest.mark.asyncio
async def test_post_event_entity_returns_dict():
    with patch.object(
        node_api, "_post_request", AsyncMock(return_value={"event_id": "EVT-0001"})
    ) as m:
        resp = await node_api.post_event_entity(
            {
                "title": "美联储议息",
                "source_type": "calendar",
                "event_start_time": "2026-09-17T00:00:00+08:00",
                "time_source": "calendar",
            }
        )
    assert resp == {"event_id": "EVT-0001"}
    url = m.call_args.args[0]
    assert url == "/internal/event-entities"
    body = m.call_args.args[1]
    assert body["title"] == "美联储议息" and body["source_type"] == "calendar"


@pytest.mark.asyncio
async def test_post_event_entity_failure_returns_none():
    # 端点未落地/失败 → None（调用方降级跳过，不抛异常）
    with patch.object(node_api, "_post_request", AsyncMock(return_value=None)):
        assert await node_api.post_event_entity({"title": "x", "source_type": "news"}) is None


@pytest.mark.asyncio
async def test_get_event_entities_returns_list():
    with patch.object(
        node_api, "get", AsyncMock(return_value={"items": [{"event_id": "EVT-0001"}]})
    ) as m:
        rows = await node_api.get_event_entities({"status": "scheduled", "date_from": "2026-09-01"})
    assert len(rows) == 1 and rows[0]["event_id"] == "EVT-0001"
    assert m.call_args.args[0] == "/internal/event-entities?status=scheduled&date_from=2026-09-01"


@pytest.mark.asyncio
async def test_get_event_entities_non_dict_returns_none():
    # 响应非 dict（失败/为空）→ 返回 None（调用方降级）
    with patch.object(node_api, "get", AsyncMock(return_value=None)):
        assert await node_api.get_event_entities() is None
