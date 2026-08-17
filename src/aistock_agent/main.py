"""FastAPI 应用入口"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI

from aistock_agent.api.middleware import setup_middleware
from aistock_agent.api.routes import health_router
from aistock_agent.api.routes import router as api_router
from aistock_agent.api.ws import router as ws_router
from aistock_agent.config import settings
from aistock_agent.observability.logging import get_logger, setup_logging
from aistock_agent.services.http_client import HttpClientPool, LlmHttpClient
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.services.scheduler import shutdown_scheduler, start_scheduler

# 配置 structlog JSON 日志（应用启动前；输出含 timestamp/level/event/request_id）
setup_logging(settings.log_level)

logger = get_logger(__name__)

# LLM httpx 连接池默认超时（对齐 llm._LLM_REQUEST_TIMEOUT_SECONDS = 600）
_LLM_HTTP_TIMEOUT = 600.0


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """应用生命周期：启动初始化资源池，关闭优雅释放。

    启动时初始化 Redis 连接池、httpx AsyncClient 与 LLM 连接池，
    任一初始化失败不崩溃（降级运行，由调用方处理异常）。
    关闭时无条件关闭所有池（close 幂等）。
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

    # LLM 连接池（ChatOpenAI 复用受限 AsyncClient，防 DeepSeek CLOSE-WAIT 堆积）
    try:
        await LlmHttpClient.init(timeout=_LLM_HTTP_TIMEOUT)
    except Exception:
        logger.error("llm_http_client_init_failed", exc_info=True)

    # 启动定时调度（在连接池初始化之后；异常不崩溃，降级为无调度运行）
    try:
        start_scheduler()
    except Exception:
        logger.error("scheduler_start_failed", exc_info=True)

    # 启动事件消费者（quick_snapshot_enabled 时）
    if settings.quick_snapshot_enabled:
        try:
            from aistock_agent.services.event_bus import EventBus, set_default_bus
            from aistock_agent.services.event_consumers import ConsumerContext, start_all_consumers
            from aistock_agent.services.redis_pool import RedisPool as _RP  # noqa: N814

            redis = await _RP.get_client()
            event_bus = EventBus(
                redis,
                max_retries=settings.event_bus_max_retries,
                deadletter_prefix=settings.event_bus_deadletter_prefix,
                consumer_group=settings.event_bus_consumer_group,
                stream_max_len=settings.event_stream_max_len,
            )
            set_default_bus(event_bus)  # 供 review.run() 双保险补发 review_done 使用
            ctx = ConsumerContext(event_bus)
            start_all_consumers(ctx)
            logger.info("event_consumers_started")
        except Exception:
            logger.error("event_consumers_start_failed", exc_info=True)

    # 启动 Stock Trace Consumer（集成模式：在主进程内运行，一次重启即可）
    # 使用独立的 aioredis 实例（stock_trace_redis_url, db=2），不复用 RedisPool 单例（db=1）
    stock_trace_consumer_task: asyncio.Task[None] | None = None
    stock_trace_redis: aioredis.Redis | None = None
    if settings.stock_trace_consumer_enabled:
        try:
            import aistock_agent.workers.stock_trace_consumer as _stc_module
            from aistock_agent.workers.stock_trace_consumer import StockTraceConsumer

            stock_trace_redis = aioredis.from_url(
                settings.stock_trace_redis_url,
                max_connections=settings.redis_max_connections,
            )
            consumer = StockTraceConsumer(stock_trace_redis)
            stock_trace_consumer_task = asyncio.create_task(consumer.run_forever())
            # 标记 consumer 已启用，供 /health/ready 检查心跳
            _stc_module._stock_trace_consumer_enabled = True
            logger.info("stock_trace_consumer_started_in_process")
        except Exception:
            logger.error("stock_trace_consumer_start_failed", exc_info=True)
            # 启动失败时清理已创建的 redis 连接
            if stock_trace_redis is not None:
                await stock_trace_redis.aclose()
                stock_trace_redis = None

    # 启动自选股洞察 Consumer（集成模式）：独立 aioredis 连接（insight_redis_url, db=3），
    # 不复用 RedisPool 单例（db=1）。worker 为真实归因 worker（Task 11，替换占位实现）。
    insight_consumer_task: asyncio.Task[None] | None = None
    insight_redis: aioredis.Redis | None = None
    if settings.insight_consumer_enabled:
        try:
            from aistock_agent.workers.insight_consumer import InsightConsumer
            from aistock_agent.workers.insight_worker import InsightWorker

            insight_redis = aioredis.from_url(  # type: ignore[no-untyped-call]
                settings.insight_redis_url,
                max_connections=settings.redis_max_connections,
            )
            insight_consumer = InsightConsumer(
                insight_redis, InsightWorker()  # type: ignore[arg-type]
            )
            insight_consumer_task = asyncio.create_task(insight_consumer.run_forever())
            logger.info("insight_consumer_started_in_process")
        except Exception:
            logger.error("insight_consumer_start_failed", exc_info=True)
            # 启动失败时清理已创建的 redis 连接
            if insight_redis is not None:
                await insight_redis.aclose()
                insight_redis = None

    yield

    # 关闭：先停 insight Consumer，再停 Stock Trace Consumer，
    # 再停事件消费者，再停调度器，最后关连接池
    if insight_consumer_task is not None:
        insight_consumer_task.cancel()
        try:
            await insight_consumer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("insight_consumer_stop_failed", exc_info=True)
    if insight_redis is not None:
        await insight_redis.aclose()
        logger.info("insight_consumer_stopped_in_process")

    if stock_trace_consumer_task is not None:
        stock_trace_consumer_task.cancel()
        try:
            await stock_trace_consumer_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("stock_trace_consumer_stop_failed", exc_info=True)
    if stock_trace_redis is not None:
        await stock_trace_redis.aclose()
        logger.info("stock_trace_consumer_stopped_in_process")

    if settings.quick_snapshot_enabled:
        try:
            from aistock_agent.services.event_bus import set_default_bus
            from aistock_agent.services.event_consumers import stop_all_consumers

            await stop_all_consumers()
            set_default_bus(None)  # 清除默认总线引用，防止关闭后泄漏
        except Exception:
            logger.error("event_consumers_stop_failed", exc_info=True)

    shutdown_scheduler()
    await RedisPool.close()
    await HttpClientPool.close()
    await LlmHttpClient.close()
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
