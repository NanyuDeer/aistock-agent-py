"""P6 独立进程运行时初始化测试（全部离线）。

根因 A：独立 CLI 进程没有走 FastAPI lifespan，`HttpClientPool._client is None`，
`NodeApiClient` 每次抛 `RuntimeError` 并被降级成 None。本文件验证
`bootstrap.ensure_runtime_pools` 能幂等初始化连接池，并把异常收敛为状态而不是
抛出裸异常；`ensure_app_env` 在构造 `Settings` 之前设置 `APP_ENV`。

不触网：只初始化 httpx 连接池对象本身，不发起任何请求。
"""

from __future__ import annotations

import os

import pytest

from aistock_agent.brain_v4.runtime.bootstrap import (
    PoolStatus,
    ensure_app_env,
    ensure_runtime_pools,
)
from aistock_agent.services.http_client import HttpClientPool, LlmHttpClient


async def _reset_pools() -> None:
    await HttpClientPool.close()
    await LlmHttpClient.close()


def test_ensure_app_env_sets_default_when_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_ENV", raising=False)

    value = ensure_app_env()

    assert value == "production"
    assert os.environ["APP_ENV"] == "production"


def test_ensure_app_env_keeps_existing_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "staging")

    assert ensure_app_env() == "staging"
    assert os.environ["APP_ENV"] == "staging"


@pytest.mark.asyncio
async def test_ensure_runtime_pools_initializes_http_client() -> None:
    await _reset_pools()
    try:
        status = await ensure_runtime_pools()

        assert isinstance(status, PoolStatus)
        assert status.http_ready is True
        assert status.error is None
        # 初始化后 NodeApiClient 依赖的 get_client 必须可用。
        client = await HttpClientPool.get_client()
        assert client is not None
        first = HttpClientPool._client

        # 幂等：重复调用不会创建第二个 client。
        again = await ensure_runtime_pools()
        assert again.http_ready is True
        assert HttpClientPool._client is first
    finally:
        await _reset_pools()


@pytest.mark.asyncio
async def test_ensure_runtime_pools_returns_status_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await _reset_pools()

    async def _boom(timeout: float = 10.0) -> None:
        raise RuntimeError("pool boom")

    monkeypatch.setattr(HttpClientPool, "init", staticmethod(_boom))

    # 不得抛出裸异常：异常必须收敛为 PoolStatus.error。
    status = await ensure_runtime_pools()

    assert status.http_ready is False
    assert status.error is not None
    assert "pool boom" in status.error
