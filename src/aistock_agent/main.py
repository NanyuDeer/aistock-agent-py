"""FastAPI 应用入口"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from aistock_agent.api.middleware import setup_middleware
from aistock_agent.api.routes import health_router
from aistock_agent.api.routes import router as api_router
from aistock_agent.api.ws import router as ws_router
from aistock_agent.config import settings
from aistock_agent.observability.logging import get_logger, setup_logging
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.services.scheduler import shutdown_scheduler, start_scheduler

# 配置 structlog JSON 日志（应用启动前；输出含 timestamp/level/event/request_id）
setup_logging(settings.log_level)

logger = get_logger(__name__)


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

    # 启动定时调度（在连接池初始化之后；异常不崩溃，降级为无调度运行）
    try:
        start_scheduler()
    except Exception:
        logger.error("scheduler_start_failed", exc_info=True)

    yield

    # 关闭：优雅释放（先停调度器，再关连接池——晨报任务依赖 Redis 缓存）
    shutdown_scheduler()
    await RedisPool.close()
    await HttpClientPool.close()
    logger.info("agent_service_stopped")


app = FastAPI(
    title="AiStock Agent Service",
    version="1.0.0",
    description="LangGraph 多Agent智能体服务",
    lifespan=lifespan,
)

# 注册中间件：request_id（最外层）→ access_log → CORS（最内层）
setup_middleware(app)

# 注册路由
app.include_router(api_router, prefix="/api/agent", tags=["agent"])
app.include_router(ws_router, prefix="/api/agent", tags=["websocket"])
# 健康检查挂载到根路径：/health（liveness）、/health/ready（readiness）
app.include_router(health_router)
