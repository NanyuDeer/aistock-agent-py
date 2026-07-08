"""HTTP 中间件 — 请求 ID 注入、结构化访问日志、CORS

中间件职责：
- request_id_middleware：从 X-Request-ID header 取或生成 UUID，绑定到 structlog
  contextvar，响应回写 header；finally 无条件清理 contextvar 防跨请求污染。
- access_log_middleware：记录 method/path/status/duration，通过 structlog 输出
  JSON 访问日志（request_id 从 contextvar 自动合并）。
- setup_middleware：统一注册上述两个中间件 + CORSMiddleware。

注册顺序（Starlette add_middleware 采用 prepend，最后注册 = 最外层）：
    1. CORSMiddleware   — 最先注册（最内层）
    2. access_log_middleware
    3. request_id_middleware — 最后注册（最外层）

request_id 在最外层确保所有请求（含 CORS 预检）都注入 request_id，
access_log 在内层通过 merge_contextvars 读取绑定的 request_id。
"""

from __future__ import annotations

import time
import uuid

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from aistock_agent.config import settings
from aistock_agent.observability.logging import get_logger

#: 请求/响应中传递 request ID 的 HTTP header 名称
REQUEST_ID_HEADER = "X-Request-ID"


async def request_id_middleware(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """注入 X-Request-ID：从请求 header 取或生成 UUID，绑定到 structlog contextvar。

    绑定后所有下游日志（含 access_log_middleware）通过 merge_contextvars
    自动携带 request_id。finally 块无条件清理 contextvar，防止跨请求污染。

    Args:
        request: Starlette Request 对象。
        call_next: 下一个中间件/路由处理函数。

    Returns:
        处理后的 Response，响应 header 中回写 X-Request-ID。
    """
    # 从 header 取或生成 UUID（客户端可传入自定义 trace ID）
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    finally:
        # 无条件清理：即使 call_next 抛异常也执行，防止 contextvar 跨请求泄漏
        structlog.contextvars.clear_contextvars()


async def access_log_middleware(
    request: Request,
    call_next: RequestResponseEndpoint,
) -> Response:
    """结构化访问日志：记录 method/path/status/duration。

    运行在 request_id_middleware 内层，通过 structlog merge_contextvars
    自动从 contextvar 读取 request_id（无需显式传递）。

    logger 在函数内获取（非模块级），确保 PrintLogger 在请求时评估 sys.stdout，
    兼容测试环境的 stdout 捕获（capsys / redirect_stdout）。

    Args:
        request: Starlette Request 对象。
        call_next: 下一个中间件/路由处理函数。

    Returns:
        处理后的 Response。
    """
    # 在函数内获取 logger：PrintLoggerFactory 延迟评估 sys.stdout，
    # 确保测试时 capsys 已替换 sys.stdout 后才创建 PrintLogger。
    logger = get_logger("aistock_agent.api.middleware.access")
    start = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        # 异常路径也记录访问日志（status=500），然后重新抛出
        duration_ms = (time.perf_counter() - start) * 1000
        logger.error(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status=500,
            duration_ms=round(duration_ms, 2),
        )
        raise
    duration_ms = (time.perf_counter() - start) * 1000
    logger.info(
        "request_completed",
        method=request.method,
        path=request.url.path,
        status=response.status_code,
        duration_ms=round(duration_ms, 2),
    )
    return response


def setup_middleware(app: FastAPI) -> None:
    """注册所有 HTTP 中间件。

    注册顺序决定执行顺序（Starlette add_middleware 采用 prepend，
    最后注册的中间件在最外层，最先处理请求）：

        1. CORSMiddleware        — 跨域预检/实际请求（最内层）
        2. access_log_middleware — 访问日志
        3. request_id_middleware — 请求 ID 注入（最外层）

    request_id 在最外层确保所有请求都注入 request_id，
    access_log 在内层能从 contextvar 读取 request_id 写入访问日志。

    Args:
        app: FastAPI 应用实例。
    """
    # 1. CORS（最先注册 = 最内层）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    # 2. 访问日志
    app.add_middleware(BaseHTTPMiddleware, dispatch=access_log_middleware)
    # 3. 请求 ID（最后注册 = 最外层）
    app.add_middleware(BaseHTTPMiddleware, dispatch=request_id_middleware)
