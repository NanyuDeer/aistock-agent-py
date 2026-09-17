"""POST /admin/trigger/review_full 的 `with_chain` 联动：复盘成功后补发 `review_done`。

背景（2026-09-17 生产踩坑）：管理员手动触发只跑 `run_review`（不走 `ReviewFullConsumer`），
**不发布 `review_done`** → 链组装/级联预判消费者永不触发，手动验证全链路必然踩坑。

覆盖：`with_chain` 缺省不发布（回归）/ true 且 status=ok 走默认总线发布一次 /
status≠ok 不发布 / 无默认总线时用 RedisPool 临时总线（临时连接关、单例不关）/
RedisPool 未初始化时按 settings.redis_url 临时连接并关闭 / 发布异常不影响已完成的复盘返回。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from aistock_agent.api.routes import router
from aistock_agent.config import settings

AUTH_HEADERS = {"X-Internal-Token": settings.internal_api_token}
ENDPOINT = "/api/agent/admin/trigger/review_full"


@pytest.fixture
def client():
    """构造 FastAPI TestClient，只挂载 router（不启动 lifespan/scheduler）。"""
    app = FastAPI()
    app.include_router(router, prefix="/api/agent")
    return TestClient(app)


def _review_result(
    status: str = "ok",
    report_date: str = "2026-09-17",
    trace_id: str = "manual-full-123",
):
    """run_review 结果的最小形状（路由只读 status/report_date/trace_id/snapshot_kind/markdown）。"""
    return type(
        "R",
        (),
        {
            "status": status,
            "report_date": report_date,
            "snapshot_kind": "full",
            "trace_id": trace_id,
            "markdown": "# Full",
        },
    )()


def _fake_redis() -> AsyncMock:
    """假 Redis 客户端：幂等 key 未命中（否则 publish 会短路成"已处理"→ 不 xadd）。"""
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    redis.setex = AsyncMock()
    redis.xgroup_create = AsyncMock()
    redis.xadd = AsyncMock(return_value=b"1-1")
    return redis


def _post(client: TestClient, payload: dict | None = None):
    return client.post(
        ENDPOINT,
        headers=AUTH_HEADERS,
        json=payload if payload is not None else {"report_date": "2026-09-17"},
    )


def test_with_chain_absent_keeps_existing_behavior(client) -> None:
    """with_chain 缺省（body 无该键）→ 不触碰总线、不发布（既有行为不变）。"""
    with (
        patch(
            "aistock_agent.agents.workers.review.run_review",
            new=AsyncMock(return_value=_review_result()),
        ),
        patch(
            "aistock_agent.services.event_bus.get_default_bus", new=MagicMock()
        ) as get_bus,
        patch(
            "aistock_agent.services.event_consumers.publish_review_done",
            new=AsyncMock(),
        ) as publish,
    ):
        resp = _post(client)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["chain_published"] is False
    get_bus.assert_not_called()
    publish.assert_not_awaited()


def test_with_chain_true_publishes_on_default_bus(client) -> None:
    """with_chain=true 且 status=ok → 用会话内默认总线发布一次（幂等 id 由 _publish 负责）。"""
    bus = MagicMock()
    with (
        patch(
            "aistock_agent.agents.workers.review.run_review",
            new=AsyncMock(return_value=_review_result()),
        ),
        patch(
            "aistock_agent.services.event_bus.get_default_bus",
            new=MagicMock(return_value=bus),
        ),
        patch(
            "aistock_agent.services.event_consumers.publish_review_done",
            new=AsyncMock(),
        ) as publish,
    ):
        resp = _post(client, {"report_date": "2026-09-17", "with_chain": True})

    assert resp.status_code == 200
    assert resp.json()["chain_published"] is True
    publish.assert_awaited_once()
    assert publish.await_args.args[0] is bus
    assert publish.await_args.kwargs == {
        "report_date": "2026-09-17",
        "trace_id": "manual-full-123",
    }


def test_with_chain_true_does_not_publish_when_status_not_ok(client) -> None:
    """status≠ok（降级/跳过）→ 不发布（对齐调度路径"仅 status=ok 发 review_done"硬约束）。"""
    with (
        patch(
            "aistock_agent.agents.workers.review.run_review",
            new=AsyncMock(return_value=_review_result(status="degraded")),
        ),
        patch(
            "aistock_agent.services.event_bus.get_default_bus",
            new=MagicMock(return_value=MagicMock()),
        ),
        patch(
            "aistock_agent.services.event_consumers.publish_review_done",
            new=AsyncMock(),
        ) as publish,
    ):
        resp = _post(client, {"report_date": "2026-09-17", "with_chain": True})

    assert resp.status_code == 200
    assert resp.json()["chain_published"] is False
    publish.assert_not_awaited()


def test_with_chain_falls_back_to_temp_bus_using_pool_client(client) -> None:
    """无默认总线（lifespan 未建消费者）→ 复用 RedisPool 单例客户端临时建总线发布；单例不关。"""
    redis = _fake_redis()
    with (
        patch(
            "aistock_agent.agents.workers.review.run_review",
            new=AsyncMock(return_value=_review_result()),
        ),
        patch(
            "aistock_agent.services.event_bus.get_default_bus",
            new=MagicMock(return_value=None),
        ),
        patch(
            "aistock_agent.services.redis_pool.RedisPool.get_client",
            new=AsyncMock(return_value=redis),
        ),
    ):
        resp = _post(client, {"report_date": "2026-09-17", "with_chain": True})

    assert resp.status_code == 200
    assert resp.json()["chain_published"] is True
    # 发布到 review_done 通道（event_id 由 publish_review_done 生成）
    channel = redis.xadd.await_args.args[0]
    assert channel == "review_done"
    redis.aclose.assert_not_awaited()  # RedisPool 单例属全局资源，不得关闭


def test_with_chain_temp_connection_from_settings_url_when_pool_missing(client) -> None:
    """RedisPool 未初始化（RuntimeError）→ 按 settings.redis_url 临时连，发布后关闭。"""
    redis = _fake_redis()
    with (
        patch(
            "aistock_agent.agents.workers.review.run_review",
            new=AsyncMock(return_value=_review_result()),
        ),
        patch(
            "aistock_agent.services.event_bus.get_default_bus",
            new=MagicMock(return_value=None),
        ),
        patch(
            "aistock_agent.services.redis_pool.RedisPool.get_client",
            new=AsyncMock(side_effect=RuntimeError("RedisPool not initialized")),
        ),
        patch("redis.asyncio.from_url", new=MagicMock(return_value=redis)) as from_url,
    ):
        resp = _post(client, {"report_date": "2026-09-17", "with_chain": True})

    assert resp.status_code == 200
    assert resp.json()["chain_published"] is True
    assert from_url.call_args.args[0] == settings.redis_url  # URL 取配置（可被 APP_ENV 覆写）
    redis.xadd.assert_awaited()
    redis.aclose.assert_awaited_once()  # 临时连接用后即关


def test_with_chain_publish_failure_does_not_affect_response(client) -> None:
    """发布链路抛异常 → 只 warning，已完成的复盘结果照常返回（chain_published=False）。"""
    with (
        patch(
            "aistock_agent.agents.workers.review.run_review",
            new=AsyncMock(return_value=_review_result()),
        ),
        patch(
            "aistock_agent.services.event_bus.get_default_bus",
            new=MagicMock(return_value=MagicMock()),
        ),
        patch(
            "aistock_agent.services.event_consumers.publish_review_done",
            new=AsyncMock(side_effect=RuntimeError("redis down")),
        ),
    ):
        resp = _post(client, {"report_date": "2026-09-17", "with_chain": True})

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["trace_id"] == "manual-full-123"
    assert body["chain_published"] is False


def test_with_chain_invalid_date_still_422(client) -> None:
    """请求体升级为带 with_chain 的模型后，report_date 校验行为不变（非法 → 422）。"""
    with patch(
        "aistock_agent.agents.workers.review.run_review", new=AsyncMock()
    ) as run:
        resp = _post(client, {"report_date": "invalid", "with_chain": True})

    assert resp.status_code == 422
    run.assert_not_awaited()
