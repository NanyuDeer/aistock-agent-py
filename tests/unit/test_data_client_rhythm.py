"""data_client 节奏大师新增方法（正常 + 降级语义）。"""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock

import pytest

from aistock_agent.services.data_client import NodeApiClient
from aistock_agent.services.http_client import HttpClientPool


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


class _InternalMirrorHandler(BaseHTTPRequestHandler):
    """迷你 app-api 镜像：{ code: 200, data: {...} } 信封（C1 对齐 internal.ts 成功约定）。"""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler 按 HTTP 动词命名)
        if self.path.startswith("/internal/calendar/events"):
            events = [{"date": "2026-09-01", "title": "英伟达财报", "importance": "high"}]
            body = {"code": 200, "data": {"events": events}}
        elif self.path.startswith("/internal/fear-greed"):
            body = {
                "code": 200,
                "data": {
                    "index": 62.5,
                    "label": "贪婪",
                    "indicators": [],
                    "history": {"dates": [], "scores": []},
                },
            }
        else:
            body = {"code": 404, "message": "not found"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        return


@pytest.mark.asyncio
async def test_real_http_unwraps_code200_envelope() -> None:
    """C1 联调锁定：真实 HTTP server + 真实 _request 解包 code==200 信封。

    app-api 侧断言 body.code===200（internalRouter/internalMirror 测试），
    本测试从消费端用真实 _request 走通 code==200 响应，双端锁定防回归。
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _InternalMirrorHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    await HttpClientPool.init()
    try:
        c = NodeApiClient()
        c._base_url = f"http://127.0.0.1:{server.server_port}"
        c._token = "test-token"
        events = await c.get_calendar_events("2026-09-01", "2026-09-05")
        assert events is not None and events[0]["title"] == "英伟达财报"
        fg = await c.get_fear_greed()
        assert fg is not None and fg["index"] == 62.5
    finally:
        await HttpClientPool.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
