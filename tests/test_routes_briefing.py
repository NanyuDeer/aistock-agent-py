"""briefing 路由测试 — morning/trigger 和 event/trigger

覆盖：
- morning trigger 无事件/多事件/缓存命中/单事件失败/降级场景
- event trigger 正常/异常/空 body
- 鉴权拒绝（无 token / 错误 token）
- 正确 token 优先级
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from aistock_agent.config import settings


# 使用实际配置的内部 token（.env 可能覆盖默认值）
AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}
WRONG_HEADERS = {"X-Internal-Token": "wrong-token"}


@pytest.fixture
def client():
    """构造 FastAPI TestClient，只挂载 router（不启动 lifespan/scheduler）"""
    from aistock_agent.api.routes import router

    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


# ── Morning trigger ──


class TestTriggerMorningBriefing:
    """POST /api/agent/briefing/morning/trigger"""

    def _mock_morning_result(
        self,
        major_events=None,
        cached=False,
        morning_generated=True,
        morning_persisted=True,
    ):
        """构造 morning_agent.run 的 mock 返回值"""
        analysis_reports = {
            "major_events": major_events or [],
            "cached": cached,
            "morning_generated": morning_generated,
            "morning_persisted": morning_persisted,
        }
        if morning_generated:
            return {
                "final_response": '{"display_report": {}}',
                "analysis_reports": analysis_reports,
            }
        else:
            return {
                "final_response": "晨报生成暂时不可用，请稍后重试",
                "analysis_reports": analysis_reports,
            }

    def _mock_event_result(self, success=True, title="测试事件", persisted=True):
        from aistock_agent.services.event_conduction import EventConductionResult

        return EventConductionResult(
            success=success,
            event_id=f"evt_{title[:8]}",
            title=title,
            event_generated=success,
            persisted=persisted if success else False,
            error=None if success else "degraded",
        )

    def test_no_major_events(self, client):
        """晨报成功但无重大事件 → event_triggered_count=0"""
        morning_result = self._mock_morning_result(major_events=[])
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            resp = client.post(
                "/api/agent/briefing/morning/trigger",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["morning_generated"] is True
        assert body["has_major_events"] is False
        assert body["major_event_count"] == 0
        assert body["event_triggered_count"] == 0
        assert body["event_succeeded_count"] == 0
        assert body["event_failed_count"] == 0

    def test_multiple_events_all_succeed(self, client):
        """多事件全部成功"""
        events = [
            {"title": "美联储加息", "summary": "加息25bp"},
            {"title": "通胀数据公布", "summary": "CPI 3.2%"},
        ]
        morning_result = self._mock_morning_result(major_events=events)
        event_results = [self._mock_event_result(True, e["title"]) for e in events]
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            with patch(
                "aistock_agent.services.event_conduction.run_event_conduction_batch",
                new_callable=AsyncMock,
                return_value=event_results,
            ):
                resp = client.post(
                    "/api/agent/briefing/morning/trigger",
                    headers=AUTH_HEADERS,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["has_major_events"] is True
        assert body["major_event_count"] == 2
        assert body["event_triggered_count"] == 2
        assert body["event_succeeded_count"] == 2
        assert body["event_failed_count"] == 0
        assert body["event_persisted_count"] == 2
        assert body["event_persist_failed_count"] == 0

    def test_cached_morning_with_major_events(self, client):
        """缓存命中的晨报含 major_events 也触发事件传导"""
        events = [{"title": "缓存事件", "summary": "测试"}]
        morning_result = self._mock_morning_result(
            major_events=events, cached=True
        )
        event_results = [self._mock_event_result(True, "缓存事件")]
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            with patch(
                "aistock_agent.services.event_conduction.run_event_conduction_batch",
                new_callable=AsyncMock,
                return_value=event_results,
            ):
                resp = client.post(
                    "/api/agent/briefing/morning/trigger",
                    headers=AUTH_HEADERS,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["cached"] is True
        assert body["major_event_count"] == 1
        assert body["event_triggered_count"] == 1
        assert body["event_succeeded_count"] == 1

    def test_single_event_failure_does_not_block_others(self, client):
        """单个事件失败不阻断其他事件"""
        events = [
            {"title": "成功事件1"},
            {"title": "失败事件"},
            {"title": "成功事件2"},
        ]
        morning_result = self._mock_morning_result(major_events=events)
        event_results = [
            self._mock_event_result(True, "成功事件1"),
            self._mock_event_result(False, "失败事件"),
            self._mock_event_result(True, "成功事件2"),
        ]
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            with patch(
                "aistock_agent.services.event_conduction.run_event_conduction_batch",
                new_callable=AsyncMock,
                return_value=event_results,
            ):
                resp = client.post(
                    "/api/agent/briefing/morning/trigger",
                    headers=AUTH_HEADERS,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["major_event_count"] == 3
        assert body["event_triggered_count"] == 3
        assert body["event_succeeded_count"] == 2
        assert body["event_failed_count"] == 1

    def test_generated_but_persist_failed_reports_partial(self, client):
        """生成成功但落库失败 → event_persisted_count < event_succeeded_count"""
        events = [
            {"title": "成功且落库成功"},
            {"title": "成功但落库失败"},
        ]
        morning_result = self._mock_morning_result(major_events=events)
        event_results = [
            self._mock_event_result(True, "成功且落库成功", persisted=True),
            self._mock_event_result(True, "成功但落库失败", persisted=False),
        ]
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            with patch(
                "aistock_agent.services.event_conduction.run_event_conduction_batch",
                new_callable=AsyncMock,
                return_value=event_results,
            ):
                resp = client.post(
                    "/api/agent/briefing/morning/trigger",
                    headers=AUTH_HEADERS,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["event_triggered_count"] == 2
        assert body["event_succeeded_count"] == 2
        assert body["event_failed_count"] == 0
        assert body["event_persisted_count"] == 1
        assert body["event_persist_failed_count"] == 1

    def test_morning_degraded_reports_failure(self, client):
        """Morning 异常降级 → success=False，不触发事件传导"""
        morning_result = self._mock_morning_result(morning_generated=False)
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            with patch(
                "aistock_agent.services.event_conduction.run_event_conduction_batch",
                new_callable=AsyncMock,
            ) as mock_batch:
                resp = client.post(
                    "/api/agent/briefing/morning/trigger",
                    headers=AUTH_HEADERS,
                )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert body["morning_generated"] is False
        mock_batch.assert_not_called()

    def test_morning_exception_returns_failure(self, client):
        """morning_agent.run 抛异常 → success=False"""
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM 不可用"),
        ):
            resp = client.post(
                "/api/agent/briefing/morning/trigger",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "LLM 不可用" in body["message"]

    def test_morning_persist_failure_reports_partial(self, client):
        """Morning 持久化失败 → morning_persisted=False，success 仍为 True（报告已生成）"""
        morning_result = self._mock_morning_result(
            major_events=[], morning_persisted=False
        )
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            resp = client.post(
                "/api/agent/briefing/morning/trigger",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["morning_generated"] is True
        assert body["morning_persisted"] is False

    def test_elapsed_seconds_present(self, client):
        """响应包含 elapsed_seconds"""
        morning_result = self._mock_morning_result(major_events=[])
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            resp = client.post(
                "/api/agent/briefing/morning/trigger",
                headers=AUTH_HEADERS,
            )
        body = resp.json()
        assert "elapsed_seconds" in body
        assert isinstance(body["elapsed_seconds"], (int, float))


# ── Event trigger ──


class TestTriggerEventBriefing:
    """POST /api/agent/briefing/event/trigger"""

    def test_endpoint_exists(self, client):
        """端点存在且返回 JSON（mock event_agent.run 避免真实 LLM 调用）"""
        mock_result = {
            "final_response": "测试播报摘要",
            "analysis_reports": {
                "event_understanding": {"summary": "测试事件"},
                "event_generated": True,
                "event_persisted": True,
                "event_cached": True,
                "event_id": "evt_test123",
            },
        }
        with patch(
            "aistock_agent.services.event_conduction.run_single_event_conduction",
            new_callable=AsyncMock,
        ) as mock_conduction:
            from aistock_agent.services.event_conduction import EventConductionResult

            mock_conduction.return_value = EventConductionResult(
                success=True,
                event_id="evt_test123",
                title="测试事件标题",
                event_generated=True,
                persisted=True,
                cached=True,
            )
            resp = client.post(
                "/api/agent/briefing/event/trigger",
                json={"event_title": "测试事件标题"},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert "message" in body
        assert body["event_id"] == "evt_test123"
        assert body["event_generated"] is True
        assert body["event_persisted"] is True
        # event_cached 必须从 result.cached 读取，禁止硬编码
        assert body["event_cached"] is True
        # 禁止虚构字段
        assert "has_display_report" not in body

    def test_empty_body_calls_conduction_with_nonempty_title(self, client):
        """空 body 时构造非空默认事件标题，实际调用 run_single_event_conduction，
        并按共享服务结果返回状态（不因空标题提前失败）。"""
        from aistock_agent.services.event_conduction import EventConductionResult

        with patch(
            "aistock_agent.services.event_conduction.run_single_event_conduction",
            new_callable=AsyncMock,
        ) as mock_conduction:
            mock_conduction.return_value = EventConductionResult(
                success=True,
                event_id="evt_default",
                title="最新重大市场事件",
                event_generated=True,
                persisted=True,
                cached=True,
                error=None,
            )
            resp = client.post(
                "/api/agent/briefing/event/trigger",
                json={},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        body = resp.json()
        # 共享服务必须被实际调用（不因空 body 跳过 Agent）
        mock_conduction.assert_called_once()
        # 共享服务收到非空 title
        called_event = mock_conduction.call_args.args[0]
        assert called_event["title"], "空 body 时必须传入非空默认标题"
        # 按共享服务结果返回状态
        assert body["success"] is True
        assert body["event_generated"] is True
        assert body["event_persisted"] is True
        assert body["event_cached"] is True

    def test_agent_failure_returns_graceful_error(self, client):
        """event conduction 抛异常时返回 success=False，HTTP 200（不抛 500）"""
        with patch(
            "aistock_agent.services.event_conduction.run_single_event_conduction",
            new_callable=AsyncMock,
            side_effect=RuntimeError("LLM 不可用"),
        ):
            resp = client.post(
                "/api/agent/briefing/event/trigger",
                json={"event_title": "测试事件"},
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is False
        assert "LLM 不可用" in body["message"]


# ── 鉴权测试 ──


class TestTriggerAuth:
    """trigger 路由鉴权测试"""

    def test_morning_trigger_no_token_rejected(self, client):
        """无 token → 403"""
        resp = client.post("/api/agent/briefing/morning/trigger")
        assert resp.status_code == 403

    def test_morning_trigger_wrong_token_rejected(self, client):
        """错误 token → 403"""
        resp = client.post(
            "/api/agent/briefing/morning/trigger",
            headers=WRONG_HEADERS,
        )
        assert resp.status_code == 403

    def test_event_trigger_no_token_rejected(self, client):
        """无 token → 403"""
        resp = client.post(
            "/api/agent/briefing/event/trigger",
            json={"event_title": "测试"},
        )
        assert resp.status_code == 403

    def test_event_trigger_wrong_token_rejected(self, client):
        """错误 token → 403"""
        resp = client.post(
            "/api/agent/briefing/event/trigger",
            json={"event_title": "测试"},
            headers=WRONG_HEADERS,
        )
        assert resp.status_code == 403

    def test_morning_trigger_correct_token_accepted(self, client):
        """正确 token → 200"""
        morning_result = {
            "final_response": '{"display_report": {}}',
            "analysis_reports": {
                "major_events": [],
                "cached": False,
                "morning_generated": True,
                "morning_persisted": True,
            },
        }
        with patch(
            "aistock_agent.agents.workers.morning.run",
            new_callable=AsyncMock,
            return_value=morning_result,
        ):
            resp = client.post(
                "/api/agent/briefing/morning/trigger",
                headers=AUTH_HEADERS,
            )
        assert resp.status_code == 200
