"""data_client 日历写删读契约（预期差闭环前置，spec §5.11/裁决 C3）。"""
import json

import httpx
import pytest

from aistock_agent.services.data_client import NodeApiClient
from aistock_agent.services.http_client import HttpClientPool


@pytest.mark.asyncio
async def test_post_calendar_event_passthrough_new_fields(monkeypatch):
    client = NodeApiClient()
    captured: dict[str, object] = {}

    async def fake_post(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"code": 0, "data": {"id": 1, "upserted": True}}

    monkeypatch.setattr(client, "_post_request", fake_post)
    body = {"event_date": "2026-10-01", "title": "x", "result": "超预期", "result_source": "auto"}
    await client.post_calendar_event(body)
    assert captured["body"] == body


@pytest.mark.asyncio
async def test_delete_calendar_event(monkeypatch):
    """回归：delete_calendar_event 必须按 self.delete() 的**真实**返回契约判定成功。

    注意：NodeApiClient.delete() 已解包信封、只返回 data（即 {deleted: ...}），
    本用例此前的 fake 却返回整个信封 {"code":0,"data":{...}}——契约写错，
    导致实现里多取一层 result["data"] 的缺陷长期未被发现（恒返回 False）。
    """
    client = NodeApiClient()
    captured: dict[str, object] = {}

    async def fake_delete(path, body):
        captured["path"] = path
        captured["body"] = body
        # 与真实 NodeApiClient.delete 契约一致：已解包，只返回 data。
        return {"deleted": True}

    monkeypatch.setattr(client, "delete", fake_delete)
    ok = await client.delete_calendar_event("2026-10-01", "测试事件")
    assert ok is True
    assert captured["path"] == "/internal/calendar/events"
    assert captured["body"] == {"event_date": "2026-10-01", "title": "测试事件"}


@pytest.mark.asyncio
async def test_delete_calendar_event_false_when_not_deleted(monkeypatch):
    """回归：deleted 为 False / 返回非 dict 时均判失败（幂等语义，不误报成功）。"""
    client = NodeApiClient()

    async def fake_delete_false(path, body):
        return {"deleted": False}

    monkeypatch.setattr(client, "delete", fake_delete_false)
    assert await client.delete_calendar_event("2026-10-01", "测试事件") is False

    async def fake_delete_none(path, body):
        return None

    monkeypatch.setattr(client, "delete", fake_delete_none)
    assert await client.delete_calendar_event("2026-10-01", "测试事件") is False


@pytest.mark.asyncio
async def test_delete_calendar_event_sends_json_body(monkeypatch):
    """缺陷③回归：带 body 的 DELETE 必须真的发出请求并携带 JSON body。

    httpx.AsyncClient.delete() 没有 json= 参数（RED 时抛 TypeError，被 NodeApiClient.delete
    的宽泛 except 吞掉 → 请求从未发出、恒返回 False，「候选 rejected 清场」链路不可用）。
    接收端 app-api DELETE /internal/calendar/events 读 req.body（internalRouter.ts:94），
    故必须走 client.request("DELETE", ..., json=body) 携带 JSON body。
    """
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(200, json={"code": 0, "data": {"deleted": True}})

    real_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def fake_get_client():
        return real_client

    monkeypatch.setattr(HttpClientPool, "get_client", fake_get_client)

    client = NodeApiClient()
    try:
        # 断言：请求真的发出且携带 JSON body（证明不再被 httpx 的 delete() 签名拒绝）。
        await client.delete_calendar_event("2026-10-01", "测试事件")
    finally:
        await real_client.aclose()
    assert captured.get("method") == "DELETE", "RED：请求从未发出（TypeError 被吞 → 恒返回 False）"
    assert captured.get("body") == {"event_date": "2026-10-01", "title": "测试事件"}


@pytest.mark.asyncio
async def test_get_calendar_events_importance_filter(monkeypatch):
    client = NodeApiClient()
    captured: list[str] = []

    async def fake_request(path):
        captured.append(path)
        return {"events": [{"date": "2026-10-01", "importance": "high"}]}

    monkeypatch.setattr(client, "_request", fake_request)
    rows = await client.get_calendar_events("2026-09-21", "2026-09-22", importance="high")
    assert "importance=high" in captured[0]
    assert rows[0]["importance"] == "high"
