"""健康检查端点端到端测试 — Task 3

验证 liveness (/health) 与 readiness (/health/ready) 的 HTTP 契约：
- /health 始终 200 + {"status": "ok"}（K8s livenessProbe，不检查依赖）
- /health/ready 检查 Redis / Node.js API / LLM（可选）连通性：
  - 全部 ok → 200 + status=ok
  - 任一失败 → 503 + status=degraded
  - LLM 默认 skipped，env HEALTH_CHECK_LLM=true 时才探测

依赖 RedisPool / HttpClientPool 通过 patch 注入 mock（lifespan 在 ASGITransport
下不运行，真实单例未初始化，故必须 mock get_client）。LLM 探测 patch
services.llm.get_quick_think（routes 内惰性导入，patch 源模块即可生效）。
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from aistock_agent.main import app

# ── 公共 fixture：注入 mock 的 Redis / HttpClient 单例 ────────────────


@pytest.fixture
def mock_deps():
    """注入 ok 状态的 Redis 与 HttpClient mock，返回可改写的命名空间。

    各测试可覆盖 ``.redis.ping`` / ``.http.get`` 的 side_effect 来模拟故障。
    """
    redis_client = AsyncMock()
    redis_client.ping = AsyncMock(return_value=True)

    http_client = AsyncMock()
    ok_resp = MagicMock()
    ok_resp.status_code = 200
    ok_resp.raise_for_status = MagicMock()  # no-op for 2xx
    http_client.get = AsyncMock(return_value=ok_resp)

    with patch("aistock_agent.api.routes.RedisPool") as mock_rp, \
         patch("aistock_agent.api.routes.HttpClientPool") as mock_hp:
        mock_rp.get_client = AsyncMock(return_value=redis_client)
        mock_hp.get_client = AsyncMock(return_value=http_client)
        yield SimpleNamespace(
            redis=redis_client, http=http_client,
            redis_pool=mock_rp, http_pool=mock_hp,
        )


async def _get(client: httpx.AsyncClient, url: str) -> httpx.Response:
    return await client.get(url)


# ── liveness ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_liveness_always_returns_ok():
    """/health 始终返回 200 + {"status":"ok"}，不触达任何依赖。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await _get(client, "/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_liveness_does_not_touch_dependencies():
    """liveness 不调用 Redis / HttpClient（即使未初始化也 200）。"""
    with patch("aistock_agent.api.routes.RedisPool") as mock_rp, \
         patch("aistock_agent.api.routes.HttpClientPool") as mock_hp:
        mock_rp.get_client = AsyncMock(side_effect=AssertionError("liveness must not touch redis"))
        mock_hp.get_client = AsyncMock(side_effect=AssertionError("liveness must not touch http"))
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await _get(client, "/health")

    assert resp.status_code == 200


# ── readiness: 全部健康 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_all_healthy_returns_200(mock_deps):
    """Redis + Node.js 均可达，LLM 默认跳过 → 200 + status=ok。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await _get(client, "/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["redis"] == "ok"
    assert body["checks"]["node_api"] == "ok"
    # LLM 默认跳过（避免消耗 token）
    assert body["checks"]["llm"] == "skipped"

    # 确认 Node.js 探测打到了 {node_api_base_url}/internal/health
    mock_deps.http.get.assert_awaited_once()
    called_url = mock_deps.http.get.await_args.args[0]
    assert called_url.endswith("/internal/health")


# ── readiness: Redis 故障 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_redis_down_returns_503(mock_deps):
    """Redis ping 抛异常 → 503 + degraded，redis 检查项标记 error。"""
    mock_deps.redis.ping = AsyncMock(side_effect=ConnectionError("redis refused"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await _get(client, "/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["redis"].startswith("error")
    # Node.js 仍被探测
    assert body["checks"]["node_api"] == "ok"


# ── readiness: Node.js 故障 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_node_api_connection_error_returns_503(mock_deps):
    """Node.js 连接失败（httpx.ConnectError）→ 503 + degraded。"""
    mock_deps.http.get = AsyncMock(side_effect=httpx.ConnectError("connection refused"))

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await _get(client, "/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["node_api"].startswith("error")
    assert body["checks"]["redis"] == "ok"


@pytest.mark.asyncio
async def test_readiness_node_api_non_2xx_returns_503(mock_deps):
    """Node.js 返回 500（raise_for_status 抛错）→ 503 + degraded。"""
    bad_resp = MagicMock()
    bad_resp.status_code = 500
    bad_resp.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "server error",
            request=httpx.Request("GET", "http://node/internal/health"),
            response=httpx.Response(500),
        )
    )
    mock_deps.http.get = AsyncMock(return_value=bad_resp)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await _get(client, "/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["node_api"].startswith("error")


# ── readiness: LLM 可选探测 ───────────────────────────────────────


@pytest.mark.asyncio
async def test_readiness_llm_checked_when_enabled_ok(mock_deps, monkeypatch):
    """HEALTH_CHECK_LLM=true 时探测 LLM，成功 → checks.llm=ok，status=ok。"""
    from aistock_agent.config import settings

    monkeypatch.setattr(settings, "health_check_llm", True)

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(return_value=object())
    with patch("aistock_agent.services.llm.get_quick_think", return_value=mock_model):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await _get(client, "/health/ready")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["checks"]["llm"] == "ok"
    mock_model.ainvoke.assert_awaited_once()


@pytest.mark.asyncio
async def test_readiness_llm_failure_returns_degraded(mock_deps, monkeypatch):
    """HEALTH_CHECK_LLM=true 且 LLM 调用失败 → 503 + degraded，llm 标记 error。"""
    from aistock_agent.config import settings

    monkeypatch.setattr(settings, "health_check_llm", True)

    mock_model = AsyncMock()
    mock_model.ainvoke = AsyncMock(side_effect=RuntimeError("llm timeout"))
    with patch("aistock_agent.services.llm.get_quick_think", return_value=mock_model):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test",
        ) as client:
            resp = await _get(client, "/health/ready")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "degraded"
    assert body["checks"]["llm"].startswith("error")
