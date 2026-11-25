"""统一事件抓取中台 — 手动触发接口路由级测试（Task 3 评审修复）

覆盖（对应评审 Important 2 / Minor 2 / Minor 3）：
- 正常触发：返回统一契约 {"success": True, "data": <run_event_scrape 结果>}
- score_date / event 参数透传
- 非法 scrape_mode：allowlist 校验，返回结构化错误而非 500
- run_event_scrape 抛异常：降级返回 {"success": False, "message": ...} 而非 500
- 鉴权失败：无 token / 错误 token → 403

mock 路径说明：
- 路由在函数内 from-import run_event_scrape，
  patch import 源 aistock_agent.services.event_scraper.run_event_scrape 即生效。
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}
WRONG_HEADERS = {"X-Internal-Token": "wrong-token"}


@pytest.fixture
def client():
    """构造 FastAPI TestClient，只挂载 router（不启动 lifespan/scheduler）。"""
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def _scrape_result(scrape_mode="full_daily", persisted=2, deduped=1, error=None):
    """构造与 run_event_scrape 真实返回一致的形状。"""
    return {
        "scrape_mode": scrape_mode,
        "persisted": persisted,
        "deduped": deduped,
        "error": error,
    }


class TestTriggerEventScrape:
    """POST /api/agent/briefing/event-scrape/trigger"""

    def test_success_returns_unified_contract(self, client):
        """正常触发 → {"success": True, "data": run_event_scrape 结果}。"""
        expected = _scrape_result("full_daily", persisted=3, deduped=2)
        with patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
            return_value=expected,
        ) as mock_run:
            resp = client.post(
                "/api/agent/briefing/event-scrape/trigger",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"] == expected
        # 缺省 scrape_mode=full_daily，score_date / event 缺省为 None
        mock_run.assert_awaited_once_with("full_daily", score_date=None, event=None)

    def test_passes_score_date_and_event(self, client):
        """score_date / event 透传给 run_event_scrape。"""
        with patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
            return_value=_scrape_result("event_triggered"),
        ) as mock_run:
            resp = client.post(
                "/api/agent/briefing/event-scrape/trigger",
                headers=AUTH_HEADERS,
                json={
                    "scrape_mode": "event_triggered",
                    "score_date": "2026-08-12",
                    "event": {"symbol": "600519"},
                },
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        mock_run.assert_awaited_once_with(
            "event_triggered",
            score_date="2026-08-12",
            event={"symbol": "600519"},
        )

    def test_unknown_scrape_mode_returns_structured_error(self, client):
        """非法 scrape_mode → {"success": False, ...}（不抛 500，run_event_scrape 不被调用）。"""
        with patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
        ) as mock_run:
            resp = client.post(
                "/api/agent/briefing/event-scrape/trigger",
                headers=AUTH_HEADERS,
                json={"scrape_mode": "bogus_mode"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "bogus_mode" in body["message"]
        mock_run.assert_not_awaited()

    def test_run_exception_degrades_gracefully(self, client):
        """run_event_scrape 抛异常 → {"success": False, "message": ...}（不抛 500）。"""
        with patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
            side_effect=RuntimeError("db down"),
        ):
            resp = client.post(
                "/api/agent/briefing/event-scrape/trigger",
                headers=AUTH_HEADERS,
                json={"scrape_mode": "intraday"},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "db down" in body["message"]

    def test_no_token_rejected(self, client):
        """无 token → 403。"""
        resp = client.post(
            "/api/agent/briefing/event-scrape/trigger",
            json={"scrape_mode": "full_daily"},
        )
        assert resp.status_code == 403

    def test_wrong_token_rejected(self, client):
        """错误 token → 403。"""
        resp = client.post(
            "/api/agent/briefing/event-scrape/trigger",
            headers=WRONG_HEADERS,
            json={"scrape_mode": "full_daily"},
        )
        assert resp.status_code == 403
