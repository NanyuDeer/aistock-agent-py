"""节奏大师 — 手动触发接口路由级测试

覆盖：
- 正常触发：返回统一契约 {"success": True, "data": {..., "rhythm_card": ...}}
- refresh_slot / report_date 透传（含两者缺省值）
- 非法 refresh_slot：allowlist 校验，返回结构化错误而非 500，且不调用调度器
- 调度器抛异常：降级返回 {"success": False, "message": ...} 而非 500
- worker 无产出（异常被吞、返回降级体或空 dict）→ success False，不透传伪成功
- report_date 非法：422
- 鉴权失败：无 token / 错误 token → 403

mock 路径说明：
- 路由在函数内 from-import _dispatch_rhythm_master，
  patch import 源 aistock_agent.services.scheduler._dispatch_rhythm_master 即生效。
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings
from aistock_agent.utils.date import shanghai_today

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}
WRONG_HEADERS = {"X-Internal-Token": "wrong-token"}
URL = "/api/agent/briefing/rhythm-master/trigger"
DISPATCH_PATH = "aistock_agent.services.scheduler._dispatch_rhythm_master"


@pytest.fixture
def client():
    """构造 FastAPI TestClient，只挂载 router（不启动 lifespan/scheduler）。"""
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def _dispatch_result(
    target_date: str = "2026-09-21", refresh_slot: str = "after_close"
) -> dict[str, object]:
    """构造与 rhythm_master.run() 真实返回一致的形状。"""
    return {
        "final_response": "{}",
        "analysis_reports": {
            "rhythm_master": {
                "schema_version": "1.0",
                "target_date": target_date,
                "basis_date": "2026-09-18",
                "refresh_slot": refresh_slot,
                "synthesis_available": True,
                "rhythm_card": {"level": "ebb", "phase": "ebb", "data_missing": []},
            }
        },
    }


class TestTriggerRhythmMaster:
    """POST /api/agent/briefing/rhythm-master/trigger"""

    def test_success_returns_unified_contract(self, client):
        """正常触发 → {"success": True, "data": 卡摘要}，参数按 (slot, report_date) 透传。"""
        with patch(
            DISPATCH_PATH, new_callable=AsyncMock, return_value=_dispatch_result()
        ) as mock_dispatch:
            resp = client.post(URL, headers=AUTH_HEADERS, json={"report_date": "2026-09-18"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["target_date"] == "2026-09-21"
        assert body["data"]["basis_date"] == "2026-09-18"
        assert body["data"]["refresh_slot"] == "after_close"
        assert body["data"]["synthesis_available"] is True
        assert body["data"]["rhythm_card"]["phase"] == "ebb"
        mock_dispatch.assert_awaited_once_with("after_close", "2026-09-18")

    def test_defaults_slot_and_uses_shanghai_today(self, client):
        """缺省 refresh_slot=after_close、缺省 report_date=上海当天。"""
        with patch(
            DISPATCH_PATH, new_callable=AsyncMock, return_value=_dispatch_result()
        ) as mock_dispatch:
            resp = client.post(URL, headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        mock_dispatch.assert_awaited_once_with("after_close", shanghai_today().isoformat())

    def test_passes_midday_slot_through(self, client):
        """refresh_slot=midday 透传，data 回显 midday。"""
        with patch(
            DISPATCH_PATH,
            new_callable=AsyncMock,
            return_value=_dispatch_result(target_date="2026-09-18", refresh_slot="midday"),
        ) as mock_dispatch:
            resp = client.post(
                URL,
                headers=AUTH_HEADERS,
                json={"refresh_slot": "midday", "report_date": "2026-09-18"},
            )
        assert resp.status_code == 200
        assert resp.json()["data"]["refresh_slot"] == "midday"
        mock_dispatch.assert_awaited_once_with("midday", "2026-09-18")

    def test_unknown_slot_returns_structured_error(self, client):
        """非法 refresh_slot → {"success": False, ...}（不抛 500，调度器不被调用）。"""
        with patch(DISPATCH_PATH, new_callable=AsyncMock) as mock_dispatch:
            resp = client.post(URL, headers=AUTH_HEADERS, json={"refresh_slot": "bogus_slot"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "bogus_slot" in body["message"]
        mock_dispatch.assert_not_awaited()

    def test_dispatch_exception_degrades_gracefully(self, client):
        """调度器抛异常 → {"success": False, "message": ...}（不抛 500）。"""
        with patch(
            DISPATCH_PATH, new_callable=AsyncMock, side_effect=RuntimeError("node down")
        ):
            resp = client.post(URL, headers=AUTH_HEADERS, json={"report_date": "2026-09-18"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "node down" in body["message"]

    def test_no_card_reports_failure_with_final_response(self, client):
        """worker 吞异常返回降级体（无 analysis_reports）→ success False 且透传文案。"""
        degraded = {"final_response": "节奏大师生成暂时不可用，请稍后重试"}
        with patch(DISPATCH_PATH, new_callable=AsyncMock, return_value=degraded):
            resp = client.post(URL, headers=AUTH_HEADERS, json={"report_date": "2026-09-18"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "暂时不可用" in body["message"]

    def test_empty_dispatch_result_reports_failure(self, client):
        """调度器返回空 dict（异常兜底）→ success False。"""
        with patch(DISPATCH_PATH, new_callable=AsyncMock, return_value={}):
            resp = client.post(URL, headers=AUTH_HEADERS, json={"report_date": "2026-09-18"})
        body = resp.json()
        assert body["success"] is False
        assert body["message"] == "节奏大师无产出"

    def test_invalid_report_date_rejected(self, client):
        """语义非法日期 → 422，且不调用调度器。"""
        with patch(DISPATCH_PATH, new_callable=AsyncMock) as mock_dispatch:
            resp = client.post(URL, headers=AUTH_HEADERS, json={"report_date": "2026-13-45"})
        assert resp.status_code == 422
        mock_dispatch.assert_not_awaited()

    def test_no_token_rejected(self, client):
        """无 token → 403。"""
        resp = client.post(URL, json={"report_date": "2026-09-18"})
        assert resp.status_code == 403

    def test_wrong_token_rejected(self, client):
        """错误 token → 403。"""
        resp = client.post(URL, headers=WRONG_HEADERS, json={"report_date": "2026-09-18"})
        assert resp.status_code == 403
