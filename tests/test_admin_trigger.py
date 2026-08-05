"""管理员 trigger 端点测试。"""

import pytest
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings
from aistock_agent.api.deps import verify_internal_token

from fastapi import FastAPI

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def test_trigger_review_quick_returns_200(client):
    """POST /api/agent/admin/trigger/review_quick 返回 200 + success。"""
    from unittest.mock import patch, AsyncMock

    with patch("aistock_agent.agents.workers.review.run_review", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = type("R", (), {
            "status": "ok", "report_date": "2026-07-30",
            "snapshot_kind": "quick", "trace_id": "t1", "markdown": "# Quick"
        })()
        response = client.post(
            "/api/agent/admin/trigger/review_quick",
            headers=AUTH_HEADERS,
            json={"report_date": "2026-07-30"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["snapshot_kind"] == "quick"


def test_trigger_review_full_returns_200(client):
    """POST /api/agent/admin/trigger/review_full 返回 200 + success。"""
    from unittest.mock import patch, AsyncMock

    with patch("aistock_agent.agents.workers.review.run_review", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = type("R", (), {
            "status": "ok", "report_date": "2026-07-30",
            "snapshot_kind": "full", "trace_id": "t2", "markdown": "# Full"
        })()
        response = client.post(
            "/api/agent/admin/trigger/review_full",
            headers=AUTH_HEADERS,
            json={"report_date": "2026-07-30"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["snapshot_kind"] == "full"


def test_trigger_evening_chain_returns_200(client):
    """POST /api/agent/admin/trigger/evening_chain 返回 200 + 链路状态。"""
    from unittest.mock import AsyncMock, patch

    with patch(
        "aistock_agent.services.scheduler._run_evening_chain_task",
        new_callable=AsyncMock,
    ) as mock_chain:
        mock_chain.return_value = {
            "status": "ok",
            "report_date": "2026-07-30",
            "stages": {
                "review": "ok",
                "market_snapshot": "ok",
                "iterate": "ok",
                "brief": "ok",
                "broadcast": "ok",
            },
        }
        response = client.post(
            "/api/agent/admin/trigger/evening_chain",
            headers=AUTH_HEADERS,
            json={"report_date": "2026-07-30"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["stages"]["broadcast"] == "ok"
    mock_chain.assert_awaited_once()


def test_trigger_review_quick_requires_auth(client):
    """无 token 返回 403。"""
    response = client.post("/api/agent/admin/trigger/review_quick", json={})
    assert response.status_code == 403


def test_trigger_review_quick_invalid_date_returns_422(client):
    """无效 report_date 返回 422。"""
    response = client.post(
        "/api/agent/admin/trigger/review_quick",
        headers=AUTH_HEADERS,
        json={"report_date": "invalid"},
    )
    assert response.status_code == 422
