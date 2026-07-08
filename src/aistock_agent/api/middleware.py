"""HTTP 中间件 — 请求 ID 注入、结构化访问日志、CORS

中间件职责：
- request_id_middleware：从 X-Request-ID header 取或生成 UUID，绑定到 structlog
  contextvar，响应回写 header；finally 无条件清理 contextvar 防跨请求污染。
  未处理异常在此捕获并返回 500 JSONResponse（携带 X-Request-ID），确保所有
  响应（含错误响应）都可追溯。
- access_log_middleware：记录 method/path/status/duration，通过 structlog 输出
  JSON 访问日志（request_id 从 contextvar 自动合并）。
- global_exception_handler：防御性全局异常处理器，确保异常穿透到
  ServerErrorMiddleware 时仍返回 JSON 而非纯文本（X-Request-ID 由
  request_id_middleware 的 try/except 保证，此 handler 是边缘场景兜底）。
- setup_middleware：统一注册上述两个中间件 + CORSMiddleware + 全局异常处理器。

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
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
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

    若 call_next 抛出未处理异常（已穿透 ExceptionMiddleware），本中间件捕获
    并返回 500 JSONResponse，确保响应携带 X-Request-ID header。原因：
    Starlette 的 ExceptionMiddleware 会跳过 Exception 类型的 handler（该
    handler 由 ServerErrorMiddleware 处理），而 ServerErrorMiddleware 位于
    用户中间件栈之外，其生成的 500 响应不流经本中间件。因此必须在此捕获，
    否则 500 响应将缺少 X-Request-ID。

    Args:
        request: Starlette Request 对象。
        call_next: 下一个中间件/路由处理函数。

    Returns:
        处理后的 Response，响应 header 中回写 X-Request-ID（含异常路径的 500）。
    """
    # 从 header 取或生成 UUID（客户端可传入自定义 trace ID）
    request_id = request.headers.get(REQUEST_ID_HEADER) or str(uuid.uuid4())
    structlog.contextvars.bind_contextvars(request_id=request_id)
    try:
        response = await call_next(request)
        response.headers[REQUEST_ID_HEADER] = request_id
        return response
    except Exception:
        # 未处理异常穿透 ExceptionMiddleware 到达此处。捕获并返回 500 响应，
        # 注入 X-Request-ID，确保所有响应（含错误响应）都可追溯。
        # 不 re-raise：若 re-raise，异常会穿透到 ServerErrorMiddleware（用户
        # 中间件栈外），其 500 响应不经过本中间件，缺少 X-Request-ID。
        error_response = JSONResponse(
            status_code=500,
            content={"detail": "Internal Server Error"},
        )
        error_response.headers[REQUEST_ID_HEADER] = request_id
        return error_response
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


async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """防御性全局异常处理器：返回 500 JSON 响应（而非纯文本）。

    Starlette 的 build_middleware_stack 将 Exception 类型的 handler 传给
    ServerErrorMiddleware（位于用户中间件栈之外），而非 ExceptionMiddleware。
    因此本 handler 仅在异常穿透 request_id_middleware 的 try/except 后才触发
    （边缘场景，如中间件基础设施自身故障），确保此时仍返回 JSON 而非纯文本。

    X-Request-ID 的注入由 request_id_middleware 的 try/except 保证（主修复），
    本 handler 不负责 X-Request-ID（ServerErrorMiddleware 的响应不流经用户中间件）。

    Args:
        request: 触发异常的 Starlette/FastAPI Request 对象。
        exc: 未被路由层捕获的异常。

    Returns:
        500 JSONResponse，body 为 ``{"detail": "Internal Server Error"}``。
    """
    return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})


def setup_middleware(app: FastAPI) -> None:
    """注册所有 HTTP 中间件 + 全局异常处理器。

    注册顺序决定执行顺序（Starlette add_middleware 采用 prepend，
    最后注册的中间件在最外层，最先处理请求）：

        1. CORSMiddleware        — 跨域预检/实际请求（最内层）
        2. access_log_middleware — 访问日志
        3. request_id_middleware — 请求 ID 注入（最外层）

    request_id 在最外层确保所有请求都注入 request_id，
    access_log 在内层能从 contextvar 读取 request_id 写入访问日志。

    全局异常处理器（防御性）：注册到 ServerErrorMiddleware，确保异常穿透
    request_id_middleware 时仍返回 JSON 而非纯文本。X-Request-ID 注入由
    request_id_middleware 的 try/except 保证（主修复）。

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
    # 防御性全局异常处理器：注册到 ServerErrorMiddleware，确保异常穿透
    # request_id_middleware 的 try/except 时仍返回 JSON（边缘场景兜底）。
    # X-Request-ID 由 request_id_middleware 的 try/except 保证（主修复）。
    app.add_exception_handler(Exception, global_exception_handler)
