"""from-trace 预测触发端点测试（PR-A/T5）。

参考 tests/test_admin_trigger.py 形态：TestClient + 函数内 patch。
覆盖：403 鉴权 / 400 日期校验（缺省/非法）/ 409 已验证拒覆盖（SPEC S6）/
ok / TraceUnavailableError → skipped（save_skipped_prediction 被调）/ llm_failed。
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}

# 端点从 data_client 模块级单例引用 node_api（routes.py L35 import），
# 测试 patch 单例方法属性即可拦截（与 data_client 单例同一对象）。
_NODE_API_LIST = "aistock_agent.services.data_client.node_api.list_predictions"
_PREDICT_FROM_TRACE = "aistock_agent.services.prediction_service.predict_from_trace"
_SAVE_SKIPPED = "aistock_agent.services.prediction_service.save_skipped_prediction"


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def test_from_trace_requires_auth(client):
    """无 token → 403。"""
    response = client.post(
        "/api/agent/internal/predictions/from-trace",
        json={"trade_date": "2026-08-14"},
    )
    assert response.status_code == 403


def test_from_trace_missing_trade_date_returns_400(client):
    """缺 trade_date → 400（必填）。"""
    response = client.post(
        "/api/agent/internal/predictions/from-trace",
        headers=AUTH_HEADERS,
        json={},
    )
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_from_trace_invalid_trade_date_returns_400(client):
    """trade_date 非法格式 → 400。"""
    response = client.post(
        "/api/agent/internal/predictions/from-trace",
        headers=AUTH_HEADERS,
        json={"trade_date": "2026/08/14"},
    )
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_from_trace_semantically_invalid_date_returns_400(client):
    """trade_date 语义非法（2026-13-45）→ 400。"""
    response = client.post(
        "/api/agent/internal/predictions/from-trace",
        headers=AUTH_HEADERS,
        json={"trade_date": "2026-13-45"},
    )
    assert response.status_code == 400
    assert "YYYY-MM-DD" in response.json()["detail"]


def test_from_trace_verified_record_conflict_returns_409(client):
    """已验证记录存在（verification 非空 dict）→ 409 拒覆盖（SPEC S6）。"""
    with patch(
        _NODE_API_LIST,
        new_callable=AsyncMock,
        return_value=[
            {
                "id": 1,
                "source_id": "review:2026-08-14",
                "verification": {"short": {"result": "hit"}},
            }
        ],
    ):
        response = client.post(
            "/api/agent/internal/predictions/from-trace",
            headers=AUTH_HEADERS,
            json={"trade_date": "2026-08-14"},
        )
    assert response.status_code == 409
    assert "已验证预测拒绝覆盖" in response.json()["detail"]


def test_from_trace_ok_returns_200(client):
    """predict_from_trace 成功 → 200 含 status/record。"""
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(
            _PREDICT_FROM_TRACE,
            new_callable=AsyncMock,
            return_value=(
                SimpleNamespace(status="ok", reason=""),
                {"id": 1, "source_id": "review:2026-08-14"},
            ),
        ):
            response = client.post(
                "/api/agent/internal/predictions/from-trace",
                headers=AUTH_HEADERS,
                json={"trade_date": "2026-08-14", "trace_id": "manual-regenerate"},
            )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["reason"] == ""
    assert body["record"]["id"] == 1


def test_from_trace_trace_unavailable_returns_skipped(client):
    """TraceUnavailableError → 200 status=skipped 且 save_skipped_prediction 被调（硬约束 7）。"""
    from aistock_agent.services.prediction_service import TraceUnavailableError

    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(
            _PREDICT_FROM_TRACE,
            new_callable=AsyncMock,
            side_effect=TraceUnavailableError("no trace available for review:2026-08-14"),
        ):
            with patch(
                _SAVE_SKIPPED,
                new_callable=AsyncMock,
                return_value={"id": 99, "status": "skipped"},
            ) as mock_skip:
                response = client.post(
                    "/api/agent/internal/predictions/from-trace",
                    headers=AUTH_HEADERS,
                    json={"trade_date": "2026-08-14"},
                )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "skipped"
    assert "no trace available" in body["reason"]
    assert body["record"]["id"] == 99
    mock_skip.assert_awaited_once_with(
        "review:2026-08-14", "no trace available for review:2026-08-14"
    )


def test_from_trace_llm_failed_returns_200_no_record(client):
    """llm_failed → 200 status=llm_failed record=None（不落库，调用方可重试）。"""
    with patch(_NODE_API_LIST, new_callable=AsyncMock, return_value=[]):
        with patch(
            _PREDICT_FROM_TRACE,
            new_callable=AsyncMock,
            return_value=(SimpleNamespace(status="llm_failed", reason="llm boom"), None),
        ):
            response = client.post(
                "/api/agent/internal/predictions/from-trace",
                headers=AUTH_HEADERS,
                json={"trade_date": "2026-08-14"},
            )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "llm_failed"
    assert body["reason"] == "llm boom"
    assert body["record"] is None
