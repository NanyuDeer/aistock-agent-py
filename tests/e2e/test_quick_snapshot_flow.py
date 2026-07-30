"""E2E 测试：通过 HTTP 端点验证 quick snapshot 触发流程。"""

import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings
from fastapi import FastAPI

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def test_e2e_admin_trigger_review_quick_end_to_end(client):
    """完整流程：POST /admin/trigger/review_quick -> run_review(quick) -> 返回结果。"""
    mock_result = type("R", (), {
        "status": "ok",
        "report_date": "2026-07-30",
        "snapshot_kind": "quick",
        "trace_id": "e2e-001",
        "markdown": "# E2E Quick Review\n\n测试内容",
    })()

    with patch("aistock_agent.agents.workers.review.run_review", new_callable=AsyncMock, return_value=mock_result):
        response = client.post(
            "/api/agent/admin/trigger/review_quick",
            headers=AUTH_HEADERS,
            json={"report_date": "2026-07-30"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["snapshot_kind"] == "quick"
    assert body["report_date"] == "2026-07-30"
    assert "E2E Quick Review" in body["markdown_preview"]


def test_e2e_admin_trigger_review_full_end_to_end(client):
    """完整流程：POST /admin/trigger/review_full -> run_review(full) -> 返回结果。"""
    mock_result = type("R", (), {
        "status": "ok",
        "report_date": "2026-07-30",
        "snapshot_kind": "full",
        "trace_id": "e2e-002",
        "markdown": "# E2E Full Review\n\n完整数据",
    })()

    with patch("aistock_agent.agents.workers.review.run_review", new_callable=AsyncMock, return_value=mock_result):
        response = client.post(
            "/api/agent/admin/trigger/review_full",
            headers=AUTH_HEADERS,
            json={"report_date": "2026-07-30"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["snapshot_kind"] == "full"
