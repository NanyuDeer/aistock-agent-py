"""中间件端到端测试 — Task 5

验证：
- X-Request-ID header 透传/生成
- CORS 预检（OPTIONS）和实际请求的 CORS header
- 访问日志输出（method/path/status/duration/request_id）
- structlog contextvar 请求结束后清理（无跨请求污染）

测试分层：
- e2e（HTTP via ASGITransport）：X-Request-ID、CORS、访问日志、跨请求隔离
- 直接调用中间件函数：contextvar 清理（含异常路径）
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest
import structlog
from starlette.requests import Request
from starlette.responses import Response

from aistock_agent.api.middleware import (
    REQUEST_ID_HEADER,
    request_id_middleware,
    setup_middleware,
)
from aistock_agent.main import app

# ── 公共 fixture ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_contextvars():
    """每个测试前后清理 structlog contextvars，防止跨测试污染。"""
    structlog.contextvars.clear_contextvars()
    yield
    structlog.contextvars.clear_contextvars()


def _make_request(
    headers: dict[str, str] | None = None,
    method: str = "GET",
    path: str = "/test",
) -> Request:
    """构造最小化的 Starlette Request 对象（用于直接测试中间件函数）。"""
    raw_headers = [
        (k.lower().encode("latin-1"), v.encode("latin-1"))
        for k, v in (headers or {}).items()
    ]
    scope: dict[str, object] = {
        "type": "http",
        "method": method,
        "path": path,
        "raw_path": path.encode("latin-1"),
        "headers": raw_headers,
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("testclient", 12345),
    }
    return Request(scope)


def _parse_access_log(stdout: str) -> list[dict[str, object]]:
    """从 captured stdout 中解析 JSON 日志行，返回 access log 条目。

    structlog JSONRenderer 每行输出一个 JSON 对象；过滤 event=request_completed。
    """
    entries: list[dict[str, object]] = []
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if data.get("event") == "request_completed":
            entries.append(data)
    return entries


# ── X-Request-ID 透传 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_request_id_passthrough_from_header():
    """客户端发送 X-Request-ID，响应 header 回写相同值。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get("/health", headers={REQUEST_ID_HEADER: "my-trace-id"})

    assert resp.status_code == 200
    assert resp.headers.get(REQUEST_ID_HEADER) == "my-trace-id"


@pytest.mark.asyncio
async def test_request_id_generated_when_missing():
    """客户端未发送 X-Request-ID，响应 header 包含生成的合法 UUID。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    generated = resp.headers.get(REQUEST_ID_HEADER)
    assert generated is not None
    # 验证是合法 UUID（parse 失败会抛 ValueError）
    uuid.UUID(generated)


@pytest.mark.asyncio
async def test_request_id_different_per_request():
    """两个无 header 的请求生成不同的 request_id。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp1 = await client.get("/health")
        resp2 = await client.get("/health")

    id1 = resp1.headers.get(REQUEST_ID_HEADER)
    id2 = resp2.headers.get(REQUEST_ID_HEADER)
    assert id1 is not None
    assert id2 is not None
    assert id1 != id2


# ── CORS ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cors_preflight_options():
    """OPTIONS 预检请求返回 CORS header（Allow-Origin/Methods/Headers）。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "Content-Type",
            },
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is not None
    allow_methods = resp.headers.get("access-control-allow-methods", "")
    assert "GET" in allow_methods
    allow_headers = resp.headers.get("access-control-allow-headers", "")
    assert "content-type" in allow_headers.lower()


@pytest.mark.asyncio
async def test_cors_actual_request():
    """实际 GET 请求带 Origin，响应包含 Access-Control-Allow-Origin。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get(
            "/health",
            headers={"Origin": "http://localhost:3000"},
        )

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") is not None


# ── 访问日志 ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_access_log_output(capsys: pytest.CaptureFixture[str]):
    """访问日志包含 method/path/status/duration。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    captured = capsys.readouterr()
    entries = _parse_access_log(captured.out)
    assert len(entries) >= 1
    entry = entries[-1]
    assert entry["method"] == "GET"
    assert entry["path"] == "/health"
    assert entry["status"] == 200
    assert "duration_ms" in entry


@pytest.mark.asyncio
async def test_access_log_includes_request_id(capsys: pytest.CaptureFixture[str]):
    """访问日志包含 request_id（来自 contextvar 绑定）。

    request_id_middleware 绑定 request_id 到 structlog contextvar，
    access_log_middleware 在内层通过 merge_contextvars 读取。
    """
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp = await client.get(
            "/health", headers={REQUEST_ID_HEADER: "log-trace-42"},
        )

    assert resp.status_code == 200
    captured = capsys.readouterr()
    entries = _parse_access_log(captured.out)
    assert len(entries) >= 1
    entry = entries[-1]
    assert entry.get("request_id") == "log-trace-42"


# ── contextvar 清理（直接调用中间件函数） ──────────────────────────


@pytest.mark.asyncio
async def test_contextvar_cleanup_after_middleware():
    """request_id_middleware 完成后 contextvar 被清理（无残留）。"""
    request = _make_request(headers={REQUEST_ID_HEADER: "cleanup-test-id"})

    async def call_next(_request: Request) -> Response:
        # 请求处理期间，request_id 应已绑定
        ctx = structlog.contextvars.get_contextvars()
        assert ctx.get("request_id") == "cleanup-test-id"
        return Response(status_code=200)

    await request_id_middleware(request, call_next)

    # 中间件完成后，contextvar 应被清理
    ctx = structlog.contextvars.get_contextvars()
    assert "request_id" not in ctx


@pytest.mark.asyncio
async def test_contextvar_cleanup_even_on_exception():
    """call_next 抛异常时，finally 仍清理 contextvar。"""
    request = _make_request(headers={REQUEST_ID_HEADER: "exc-id"})

    async def call_next(_request: Request) -> Response:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await request_id_middleware(request, call_next)

    ctx = structlog.contextvars.get_contextvars()
    assert "request_id" not in ctx


@pytest.mark.asyncio
async def test_no_cross_request_pollution():
    """连续两个请求，第二个请求不继承第一个的 request_id。"""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test",
    ) as client:
        resp1 = await client.get(
            "/health", headers={REQUEST_ID_HEADER: "first-request"},
        )
        resp2 = await client.get("/health")  # 不带 header

    assert resp1.headers.get(REQUEST_ID_HEADER) == "first-request"
    id2 = resp2.headers.get(REQUEST_ID_HEADER)
    assert id2 is not None
    assert id2 != "first-request"


# ── setup_middleware 注册 ──────────────────────────────────────────


def test_setup_middleware_registers_without_error():
    """setup_middleware 在新 FastAPI app 上注册不报错，中间件数量 >= 3。"""
    from fastapi import FastAPI

    new_app = FastAPI()
    setup_middleware(new_app)
    # CORSMiddleware + access_log + request_id = 3 个用户中间件
    assert len(new_app.user_middleware) >= 3
