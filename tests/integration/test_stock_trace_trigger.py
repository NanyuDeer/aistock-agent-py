"""stock_trace trigger 路由集成测试

验证：
- 403：无 Token 时返回 403
- 422：非法 symbol、额外字段
- completed：alert.run 成功 + save 成功
- degraded：save 失败或 alert.run 返回空时返回 degraded
- review.run、run_review 调用数始终为 0
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from httpx import AsyncClient, ASGITransport

from aistock_agent.api.routes import router
from aistock_agent.config import settings

_INTERNAL_TOKEN = settings.internal_api_token

_TRIGGER_PATH = "/trace/stock/trigger"

# mock 目标
_ALERT_RUN = "aistock_agent.agents.workers.alert.run"
_SAVE_REPORT = "aistock_agent.services.data_client.node_api.save_analysis_report"
_REVIEW_RUN = "aistock_agent.agents.workers.review.run"
_RUN_REVIEW = "aistock_agent.agents.workers.review.run_review"


@pytest.fixture
def app():
    """构造只挂载 stock trace 路由的最小 FastAPI 应用"""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    """httpx AsyncClient"""
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_403_without_token(client):
    """无 Token 返回 403"""
    resp = await client.post(_TRIGGER_PATH, json={"symbol": "600519"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_422_invalid_symbol(client):
    """非法 symbol 返回 422"""
    resp = await client.post(
        _TRIGGER_PATH,
        json={"symbol": "6005"},  # 不足 6 位
        headers={"X-Internal-Token": _INTERNAL_TOKEN},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_422_extra_field(client):
    """额外字段返回 422"""
    resp = await client.post(
        _TRIGGER_PATH,
        json={"symbol": "600519", "extra_field": "not_allowed"},
        headers={"X-Internal-Token": _INTERNAL_TOKEN},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_completed_flow(client):
    """正常流程：alert.run 成功 + save 成功 → status=completed"""
    with (
        patch(_ALERT_RUN) as mock_alert_run,
        patch(_SAVE_REPORT) as mock_save,
        patch(_REVIEW_RUN) as mock_review_run,
        patch(_RUN_REVIEW) as mock_run_review,
    ):
        mock_alert_run.return_value = {
            "final_response": '{"display_report":{"summary":"异动结论"},"podcast_brief":"异动摘要"}',
            "analysis_reports": {"alert": "分析结果"},
            "trace_id": "my-trace-id",
            "trace_persisted": True,
            "report_id": 42,
        }
        mock_save.return_value = {"id": 42, "report_type": "alert"}

        resp = await client.post(
            _TRIGGER_PATH,
            json={"symbol": "600519", "cycle": "short", "trace_id": "my-trace-id"},
            headers={"X-Internal-Token": _INTERNAL_TOKEN},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["trace_id"] == "my-trace-id"
    assert body["symbol"] == "600519"
    assert body["status"] == "completed"
    assert body["report_id"] == 42

    # 验证 review.run / run_review 未被调用
    mock_review_run.assert_not_called()
    mock_run_review.assert_not_called()


@pytest.mark.asyncio
async def test_degraded_when_save_fails(client):
    """save 失败时返回 degraded"""
    with (
        patch(_ALERT_RUN) as mock_alert_run,
        patch(_SAVE_REPORT) as mock_save,
    ):
        mock_alert_run.return_value = {
            "final_response": '{"display_report":{"summary":"异动结论"}}',
            "analysis_reports": {"alert": "分析结果"},
        }
        mock_save.return_value = None  # 保存失败

        resp = await client.post(
            _TRIGGER_PATH,
            json={"symbol": "000001"},
            headers={"X-Internal-Token": _INTERNAL_TOKEN},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["symbol"] == "000001"
    assert body["report_id"] is None
    assert body["degraded_reason"] is not None


@pytest.mark.asyncio
async def test_degraded_when_alert_run_empty(client):
    """alert.run 返回空 final_response 时返回 degraded"""
    with (
        patch(_ALERT_RUN) as mock_alert_run,
        patch(_SAVE_REPORT) as mock_save,
    ):
        mock_alert_run.return_value = {
            "final_response": "",
            "analysis_reports": {},
        }
        mock_save.return_value = {"id": 1}  # 这种情况下不应该走到 save

        resp = await client.post(
            _TRIGGER_PATH,
            json={"symbol": "600519"},
            headers={"X-Internal-Token": _INTERNAL_TOKEN},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "degraded"
    # 空响应时，不尝试保存
    mock_save.assert_not_called()


@pytest.mark.asyncio
async def test_uses_shanghai_today_by_default(client):
    """缺 report_date 时使用上海当天日期"""
    with (
        patch(_ALERT_RUN, return_value={
            "final_response": '{"display_report":{}}',
            "analysis_reports": {"alert": "ok"},
            "trace_id": "auto",
            "trace_persisted": True,
            "report_id": 1,
        }),
        patch(_SAVE_REPORT, return_value={"id": 1}),
    ):
        resp = await client.post(
            _TRIGGER_PATH,
            json={"symbol": "600519"},
            headers={"X-Internal-Token": _INTERNAL_TOKEN},
        )

    assert resp.status_code == 200
    body = resp.json()
    # report_date 格式为 YYYY-MM-DD
    import re
    assert re.match(r"^\d{4}-\d{2}-\d{2}$", body["report_date"])


@pytest.mark.asyncio
async def test_review_not_called(client):
    """确认 review.run / run_review 调用数始终为 0"""
    with (
        patch(_ALERT_RUN) as mock_alert_run,
        patch(_SAVE_REPORT) as mock_save,
        patch(_REVIEW_RUN) as mock_review_run,
        patch(_RUN_REVIEW) as mock_run_review,
    ):
        mock_alert_run.return_value = {
            "final_response": '{"display_report":{"summary":"ok"}}',
            "analysis_reports": {"alert": "ok"},
            "trace_id": "trace-review-check",
            "trace_persisted": True,
            "report_id": 1,
        }
        mock_save.return_value = {"id": 1}

        await client.post(
            _TRIGGER_PATH,
            json={"symbol": "600519"},
            headers={"X-Internal-Token": _INTERNAL_TOKEN},
        )

    mock_review_run.assert_not_called()
    mock_run_review.assert_not_called()
