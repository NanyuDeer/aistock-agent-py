"""DELETE /api/agent/internal/chat/threads/{session_id} 路由测试（Phase 5 Task 2）

覆盖：
- 无 token / 错 token → 403（verify_internal_token 在业务前拦截）
- 合法 token 删除 → 200，且调用 checkpointer.delete_thread(session_id)
- 幂等：连续删两次 → 200
- session_id 非法（含空格 / 超长）→ 400，不调用 delete_thread
- delete_thread 意外异常 → 500

参照 tests/test_routes_briefing.py 模式：TestClient 只挂载 router（不启动 lifespan）。
"""
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}
WRONG_HEADERS = {"X-Internal-Token": "wrong-token"}

_DELETE_URL = "/api/agent/internal/chat/threads/sess_123"


@pytest.fixture()
def client():
    """构造 FastAPI TestClient，只挂载 router（与 test_routes_briefing 一致）"""
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def test_delete_thread_missing_token_403(client):
    """缺失 X-Internal-Token → 403（delete_thread 不应被调用）"""
    with patch("aistock_agent.api.routes.delete_thread") as mock_delete:
        resp = client.delete(_DELETE_URL)
    assert resp.status_code == 403
    mock_delete.assert_not_called()


def test_delete_thread_wrong_token_403(client):
    """X-Internal-Token 不匹配 → 403"""
    with patch("aistock_agent.api.routes.delete_thread") as mock_delete:
        resp = client.delete(_DELETE_URL, headers=WRONG_HEADERS)
    assert resp.status_code == 403
    mock_delete.assert_not_called()


def test_delete_thread_valid_token_calls_delete_thread(client):
    """合法 token → 200，且 delete_thread 被调用并传入 session_id"""
    with patch("aistock_agent.api.routes.delete_thread") as mock_delete:
        resp = client.delete(_DELETE_URL, headers=AUTH_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("success") is True
    mock_delete.assert_called_once_with("sess_123")


def test_delete_thread_idempotent(client):
    """连续删两次 → 两次均 200（幂等）"""
    with patch("aistock_agent.api.routes.delete_thread") as mock_delete:
        resp1 = client.delete(_DELETE_URL, headers=AUTH_HEADERS)
        resp2 = client.delete(_DELETE_URL, headers=AUTH_HEADERS)
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert mock_delete.call_count == 2


@pytest.mark.parametrize(
    "bad_session_id",
    [
        "has space123",  # 含空格（URL 编码后解码回空格）
        "a" * 65,  # 超长（>64）
    ],
)
def test_delete_thread_invalid_session_id_400(client, bad_session_id):
    """session_id 非法（格式/长度不符）→ 400，不调用 delete_thread"""
    from urllib.parse import quote

    with patch("aistock_agent.api.routes.delete_thread") as mock_delete:
        resp = client.delete(
            f"/api/agent/internal/chat/threads/{quote(bad_session_id, safe='')}",
            headers=AUTH_HEADERS,
        )
    assert resp.status_code == 400
    mock_delete.assert_not_called()


def test_delete_thread_unexpected_error_500(client):
    """delete_thread 意外异常 → 500（app-api 侧 catch warning，"永不 500"由调用侧保证）"""
    with patch(
        "aistock_agent.api.routes.delete_thread", side_effect=RuntimeError("boom")
    ):
        resp = client.delete(_DELETE_URL, headers=AUTH_HEADERS)
    assert resp.status_code == 500
