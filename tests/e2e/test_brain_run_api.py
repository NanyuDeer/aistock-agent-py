"""POST /api/agent/brain/run —— 开关 + 鉴权 + 响应整形（B 段）。

鉴权复用 ``api.deps.verify_internal_token``（与 /chat/message 同一套 403 语义）；
关闭态返回 404，开启态透传 BrainRunResult 的六个字段。
"""
import httpx
import pytest

from aistock_agent.api import routes as routes_module
from aistock_agent.config import settings
from aistock_agent.graph.brain.orchestrator import BrainRunResult
from aistock_agent.main import app

_URL = "/api/agent/brain/run"
_HEADERS = {"X-Internal-Token": settings.internal_api_token}


async def _post(headers: dict[str, str] | None = None) -> httpx.Response:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        return await client.post(_URL, json={"subject_code": "000001"}, headers=headers or {})


@pytest.mark.asyncio
async def test_brain_run_hidden_when_disabled(monkeypatch):
    """关闭态对外表现为 404，且绝不执行主图。"""
    monkeypatch.setattr(settings, "brain_enabled", False)

    async def boom(**kwargs):
        raise AssertionError("disabled 时不得调用 run_orchestrator")

    monkeypatch.setattr(routes_module, "run_orchestrator", boom)
    resp = await _post(_HEADERS)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_brain_run_requires_token(monkeypatch):
    """开启态仍受 X-Internal-Token 保护。"""
    monkeypatch.setattr(settings, "brain_enabled", True)
    resp = await _post()
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_brain_run_returns_result(monkeypatch):
    """开启态 + 正确 token：透传 run_id/partial/record_id/errors/judgment/brief。"""
    monkeypatch.setattr(settings, "brain_enabled", True)
    captured: dict[str, object] = {}

    async def fake_run(**kwargs) -> BrainRunResult:
        captured.update(kwargs)
        return BrainRunResult(
            run_id="run_2026-11-25_000001",
            partial=False,
            judgment=None,
            brief={"schema_version": "brief.v1", "brief_type": "judgment"},
            record_id=42,
            errors=[],
        )

    monkeypatch.setattr(routes_module, "run_orchestrator", fake_run)
    resp = await _post(_HEADERS)
    assert resp.status_code == 200
    body = resp.json()
    assert body["run_id"] == "run_2026-11-25_000001"
    assert body["record_id"] == 42
    assert body["partial"] is False
    assert body["errors"] == []
    assert body["judgment"] is None
    assert body["brief"]["schema_version"] == "brief.v1"
    assert captured["subject_code"] == "000001"
