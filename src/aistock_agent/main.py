"""FastAPI 应用入口"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aistock_agent.api.routes import router as api_router
from aistock_agent.api.ws import router as ws_router
from aistock_agent.config import settings
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool

structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.set_exc_info,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ],
)

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动初始化资源池，关闭优雅释放。

    启动时初始化 Redis 连接池和 httpx AsyncClient，
    任一初始化失败不崩溃（降级运行，由调用方处理异常）。
    关闭时无条件关闭两个池（close 幂等）。
    """
    logger.info("agent_service_started", host=settings.host, port=settings.port)

    # 启动：初始化连接池（异常不崩溃，降级运行）
    try:
        await RedisPool.init(
            settings.redis_url,
            max_connections=settings.redis_max_connections,
        )
    except Exception:
        logger.error("redis_pool_init_failed", exc_info=True)

    try:
        await HttpClientPool.init(timeout=settings.http_timeout_seconds)
    except Exception:
        logger.error("http_client_pool_init_failed", exc_info=True)

    yield

    # 关闭：优雅释放（close 幂等，未初始化也安全）
    await RedisPool.close()
    await HttpClientPool.close()
    logger.info("agent_service_stopped")


app = FastAPI(
    title="AiStock Agent Service",
    version="1.0.0",
    description="LangGraph 多Agent智能体服务",
    lifespan=lifespan,
)

# CORS（Node.js 反代时需要）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(api_router, prefix="/api/agent", tags=["agent"])
app.include_router(ws_router, prefix="/api/agent", tags=["websocket"])


@app.get("/health")
async def health() -> dict[str, str]:
    """健康检查"""
    return {"status": "ok", "service": "aistock-agent"}
