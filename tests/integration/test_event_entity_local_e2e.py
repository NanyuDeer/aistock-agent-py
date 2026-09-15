"""Event Entity 本地真实 HTTP 联调探针（记忆 #59：跨仓信封 code 对齐须真实联调）。

默认 SKIP：显式设置 `EVENT_ENTITY_E2E=1` + `NODE_API_BASE_URL`(本地 app-api) +
`INTERNAL_API_TOKEN`(=app-api .env 的 INTERNAL_API_TOKEN) 才运行。

证据产出：
- POST /internal/event-entities 信封 code==200 → event_id/event_status 就位（scheduled）；
- 幂等重放：书写噪声不同的同事件标题 → 同 event_id（canonical_key ON CONFLICT）；
- GET ?status= 过滤读时重算（未来 scheduled / 已发生 occurred）；
- 降级契约：非法 body → app-api 400 → agent-py `post_event_entity` 返回 None。
"""

import os
from datetime import timedelta

import pytest
import pytest_asyncio

from aistock_agent.utils.date import shanghai_today

_E2E = os.environ.get("EVENT_ENTITY_E2E") == "1"
_SKIP_REASON = (
    "local E2E requires EVENT_ENTITY_E2E=1 + local app-api "
    "(NODE_API_BASE_URL/INTERNAL_API_TOKEN)"
)


@pytest_asyncio.fixture(autouse=True)
async def _http_pool():
    """真实 HTTP 联调需初始化 httpx 连接池（生产由 main.py lifespan 做）。"""
    from aistock_agent.services.http_client import HttpClientPool

    await HttpClientPool.init()
    yield
    await HttpClientPool.close()


def _future_date() -> str:
    return (shanghai_today() + timedelta(days=10)).isoformat()


def _past_date() -> str:
    return (shanghai_today() - timedelta(days=10)).isoformat()


@pytest.mark.skipif(not _E2E, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_event_entity_e2e_envelope_idempotency_and_status():
    from aistock_agent.services.data_client import node_api

    future = _future_date()
    resp1 = await node_api.post_event_entity(
        {
            "title": f"华为将于 {future} 发布新品",
            "source_type": "news",
            "event_start_time": f"{future}T00:00:00+08:00",
            "time_source": "news_extraction",
            "time_confidence": 0.9,
        }
    )
    # 信封 code==200 → data 为 dict
    assert resp1 is not None and resp1.get("event_id")
    # 未来事件读时重算 scheduled
    assert resp1.get("event_status") == "scheduled"

    # 幂等重放：书写噪声不同（空格/叹号）→ canonical_key 归一 → 同 event_id
    resp2 = await node_api.post_event_entity(
        {
            "title": f"华为将于 {future}  发布新品！",
            "source_type": "news",
            "event_start_time": f"{future}T00:00:00+08:00",
            "time_source": "news_extraction",
            "time_confidence": 0.9,
        }
    )
    assert resp2 is not None and resp2["event_id"] == resp1["event_id"]

    # GET ?status=scheduled 过滤（读时重算）→ 命中
    rows = await node_api.get_event_entities({"status": "scheduled"})
    assert rows is not None
    assert any(r["event_id"] == resp1["event_id"] for r in rows)

    # 已发生事件 → occurred
    past = _past_date()
    resp3 = await node_api.post_event_entity(
        {
            "title": f"{past} 已发生复盘事件",
            "source_type": "news",
            "event_start_time": f"{past}T00:00:00+08:00",
            "time_source": "news_extraction",
            "time_confidence": 0.9,
        }
    )
    assert resp3 is not None and resp3["event_id"]
    assert resp3.get("event_status") == "occurred"


@pytest.mark.skipif(not _E2E, reason=_SKIP_REASON)
@pytest.mark.asyncio
async def test_event_entity_e2e_degradation_contract():
    """非法 body → app-api 400 → post_event_entity 返回 None（调用方降级，不抛）。"""
    from aistock_agent.services.data_client import node_api

    resp = await node_api.post_event_entity(
        {"title": "", "source_type": "news", "event_start_time": "2026-09-23"}
    )
    assert resp is None
