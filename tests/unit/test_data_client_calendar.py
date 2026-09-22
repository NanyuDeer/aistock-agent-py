"""data_client 日历写删读契约（预期差闭环前置，spec §5.11/裁决 C3）。"""
import pytest

from aistock_agent.services.data_client import NodeApiClient


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
    client = NodeApiClient()
    captured: dict[str, object] = {}

    async def fake_delete(path, body):
        captured["path"] = path
        captured["body"] = body
        return {"code": 0, "data": {"deleted": True}}

    monkeypatch.setattr(client, "delete", fake_delete)
    ok = await client.delete_calendar_event("2026-10-01", "测试事件")
    assert ok is True
    assert captured["path"] == "/internal/calendar/events"
    assert captured["body"] == {"event_date": "2026-10-01", "title": "测试事件"}


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
