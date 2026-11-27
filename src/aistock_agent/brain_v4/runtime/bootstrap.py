"""独立进程（CLI / Worker）运行时初始化。

为什么需要本模块：`HttpClientPool` 与 `LlmHttpClient` 的单例只由 FastAPI
lifespan 初始化；独立 CLI 进程没有 lifespan，`get_client()` 会抛
`RuntimeError: HttpClientPool not initialized`，`NodeApiClient` 把异常吞成
None，导致取数与归档全空。这里提供幂等初始化，并把任何异常收敛为
`PoolStatus`，绝不让初始化失败抛出裸异常。

`ensure_app_env` 必须在构造 `Settings` / `NodeApiClient` 之前调用：settings
按 `APP_ENV` 读取 `.env.{APP_ENV}`，独立进程默认需要 `production`。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import structlog

logger = structlog.get_logger()

_DEFAULT_APP_ENV = "production"


@dataclass(frozen=True)
class PoolStatus:
    """运行时连接池初始化结果；失败只记录原因，不抛异常。"""

    http_ready: bool
    llm_ready: bool
    error: str | None = None


def ensure_app_env(default: str = _DEFAULT_APP_ENV) -> str:
    """当 `APP_ENV` 未设置时设置默认值，返回最终值。

    必须在导入/构造 `Settings` 之前调用，否则 settings 已经按旧的
    `APP_ENV` 读取了 `.env.*`，再改环境变量不会重新加载。
    """
    value = os.environ.get("APP_ENV")
    if not value:
        os.environ["APP_ENV"] = default
        value = default
    return value


async def ensure_runtime_pools(*, env: str | None = None) -> PoolStatus:
    """幂等初始化 Node / LLM 连接池，返回状态而不抛异常。"""
    if env is not None:
        os.environ["APP_ENV"] = env

    http_ready = False
    try:
        from aistock_agent.services.http_client import HttpClientPool

        # `HttpClientPool.init` 自身幂等：重复调用不会创建第二个 client。
        await HttpClientPool.init()
        http_ready = True
    except Exception as exc:  # noqa: BLE001 - 初始化失败必须降级为状态
        error = f"{type(exc).__name__}: {exc}"
        logger.error("brain_v4_pool_init_failed", pool="http", error=error)
        return PoolStatus(http_ready=False, llm_ready=False, error=error)

    llm_ready = False
    try:
        from aistock_agent.services.http_client import LlmHttpClient

        init = getattr(LlmHttpClient, "init", None)
        if callable(init) and getattr(LlmHttpClient, "_client", None) is None:
            await init()
        llm_ready = getattr(LlmHttpClient, "_client", None) is not None
    except Exception as exc:  # noqa: BLE001 - LLM 池失败不影响 Node 取数
        error = f"{type(exc).__name__}: {exc}"
        logger.error("brain_v4_pool_init_failed", pool="llm", error=error)
        return PoolStatus(http_ready=http_ready, llm_ready=False, error=error)

    return PoolStatus(http_ready=http_ready, llm_ready=llm_ready, error=None)


__all__ = ["PoolStatus", "ensure_app_env", "ensure_runtime_pools"]
