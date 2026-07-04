"""FastAPI 应用入口"""

from typing import Any

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from aistock_agent.api.routes import router as api_router
from aistock_agent.api.ws import router as ws_router
from aistock_agent.config import settings

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

app = FastAPI(
    title="AiStock Agent Service",
    version="1.0.0",
    description="LangGraph 多Agent智能体服务",
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
async def health() -> dict[str, Any]:
    """健康检查"""
    return {"status": "ok", "service": "aistock-agent"}


@app.on_event("startup")
async def startup() -> None:
    logger.info("agent_service_started", host=settings.host, port=settings.port)
