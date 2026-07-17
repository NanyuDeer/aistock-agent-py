"""briefing 路由测试 — morning/trigger 和 event/trigger"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """构造 FastAPI TestClient，只挂载 router（不启动 lifespan/scheduler）"""
    from aistock_agent.api.routes import router

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


class TestTriggerEventBriefing:
    """POST /api/agent/briefing/event/trigger"""

    def test_endpoint_exists(self, client):
        """端点存在且返回 JSON（mock event_agent.run 避免真实 LLM 调用）"""
        mock_result = {
            "final_response": "测试播报摘要",
            "analysis_reports": {"event_display_report": {"eventId": "evt_test"}},
        }
        with patch(
            "aistock_agent.agents.workers.event.run",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post(
                "/api/agent/briefing/event/trigger",
                json={"event_title": "测试事件标题"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "message" in body
        assert "event_id" in body
        assert body["has_display_report"] is True

    def test_empty_body_uses_default_message(self, client):
        """空 body 时用默认事件描述消息触发"""
        mock_result = {
            "final_response": "默认事件分析",
            "analysis_reports": {},
        }
        with patch(
            "aistock_agent.agents.workers.event.run",
            new_callable=AsyncMock,
            return_value=mock_result,
        ):
            resp = client.post("/api/agent/briefing/event/trigger", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["has_display_report"] is False

    def test_agent_failure_returns_graceful_error(self, client):
        """event_agent.run 抛异常时返回 success=False，HTTP 200（不抛 500）"""
        with patch(
            "aistock_agent.agents.workers.event.run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM 不可用"),
        ):
            resp = client.post(
                "/api/agent/briefing/event/trigger",
                json={"event_title": "测试事件"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "LLM 不可用" in body["message"]
