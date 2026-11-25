"""POST /admin/trigger/midday 手动补跑端点测试。

对齐同仓库 tests/test_admin_trigger.py 的 app/test-client + 内部 token 基建，
仅 mock 调度任务函数，不依赖真实 LLM / Redis / Node。
"""

from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}
PREFIX = "/api/agent"
ENDPOINT = f"{PREFIX}/admin/trigger/midday"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix=PREFIX)
    return TestClient(app)


def test_trigger_midday_returns_ok():
    """POST /admin/trigger/midday 返回 200 + 透传 status/report_date。"""
    client = _client()
    with patch(
        "aistock_agent.services.scheduler._run_midday_task",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = {"status": "ok", "report_date": "2026-08-24"}
        response = client.post(
            ENDPOINT,
            headers=AUTH_HEADERS,
            json={"report_date": "2026-08-24"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["report_date"] == "2026-08-24"
    assert "trace_id" in body
    assert "elapsed_seconds" in body
    mock_run.assert_awaited_once_with(report_date="2026-08-24")


def test_trigger_midday_requires_auth():
    """无内部 token 返回 403。"""
    client = _client()
    response = client.post(ENDPOINT, json={"report_date": "2026-08-24"})
    assert response.status_code == 403


def test_trigger_midday_invalid_date_returns_422():
    """无效 report_date 返回 422。"""
    client = _client()
    response = client.post(
        ENDPOINT,
        headers=AUTH_HEADERS,
        json={"report_date": "invalid"},
    )
    assert response.status_code == 422


def test_trigger_midday_defaults_report_date():
    """未传 report_date 时缺省使用当天，并将任务结果透传。"""
    client = _client()
    with patch(
        "aistock_agent.services.scheduler._run_midday_task",
        new_callable=AsyncMock,
    ) as mock_run:
        mock_run.return_value = {"status": "ok", "report_date": "2026-08-24"}
        response = client.post(ENDPOINT, headers=AUTH_HEADERS, json={})

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["report_date"] == "2026-08-24"
    mock_run.assert_awaited_once()
