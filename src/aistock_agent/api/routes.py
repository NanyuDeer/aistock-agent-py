"""REST 接口 — 对话消息、晨报、工具列表、健康检查"""

import json
from collections.abc import AsyncGenerator

import structlog
from fastapi import APIRouter, Depends, Response
from sse_starlette.sse import EventSourceResponse

from aistock_agent.agents.workers import morning as morning_agent
from aistock_agent.api.deps import build_initial_state, verify_internal_token
from aistock_agent.config import settings
from aistock_agent.constants import SSEEventType
from aistock_agent.graph.builder import compile_graph
from aistock_agent.schemas.chat import ChatRequest, ChatResponse
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.utils.sse import map_langgraph_event_to_sse

router = APIRouter()

# 健康检查路由（在 main.py 挂载到根路径，不在 /api/agent 前缀下）
health_router = APIRouter(tags=["health"])
_health_logger = structlog.get_logger()


@router.post("/chat/message", response_model=ChatResponse)
async def chat_message(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> ChatResponse:
    """对话消息（非流式）"""
    graph = compile_graph()

    session_id = req.session_id or f"session_{id(req)}"

    initial_state = build_initial_state(
        message=req.message,
        session_id=session_id,
        user_id=req.user_id,
        favorites=req.favorites,
    )

    result = await graph.ainvoke(
        initial_state,
        config={"configurable": {"thread_id": session_id}},
    )

    content = result.get("final_response") or "抱歉，我暂时无法处理您的请求。"
    return ChatResponse(content=content, session_id=session_id)


@router.post("/chat/stream")
async def chat_stream(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> EventSourceResponse:
    """对话消息（SSE 流式）

    走 ``graph.astream_events(version="v2")``，用 ``map_langgraph_event_to_sse``
    统一映射。相比 ``morning_agent.stream`` 多一层节点过滤：supervisor 节点产出
    的是意图分类 JSON（非用户回复），不应作为 TEXT 转发给前端。
    """
    graph = compile_graph()

    session_id = req.session_id or f"session_{id(req)}"
    initial_state = build_initial_state(
        message=req.message,
        session_id=session_id,
        user_id=req.user_id,
        favorites=req.favorites,
    )

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        _llm_started = False
        try:
            async for event in graph.astream_events(
                initial_state,
                version="v2",
                config={"configurable": {"thread_id": session_id}},
            ):
                # 过滤 supervisor 节点事件（意图分类输出不给前端）
                node = event.get("metadata", {}).get("langgraph_node")
                if node == "supervisor":
                    continue

                sse_event = map_langgraph_event_to_sse(event)
                if sse_event is None:
                    continue

                event_t = sse_event.get("type")
                if event_t in (SSEEventType.TOOL_START, SSEEventType.TOOL_END):
                    yield {"data": json.dumps(sse_event, ensure_ascii=False)}
                elif event_t == SSEEventType.TEXT:
                    # llm_start 仅在首个文本 chunk 时发射一次（有状态，保留在 generator 内）
                    if not _llm_started:
                        _llm_started = True
                        yield {"data": json.dumps(
                            {"type": SSEEventType.LLM_START, "label": "正在生成回复"},
                            ensure_ascii=False,
                        )}
                    yield {"data": json.dumps(sse_event, ensure_ascii=False)}

            yield {"data": json.dumps({"type": SSEEventType.DONE}, ensure_ascii=False)}
        except Exception as e:
            yield {"data": json.dumps(
                {"type": SSEEventType.ERROR, "message": str(e)},
                ensure_ascii=False,
            )}

    return EventSourceResponse(generator())


@router.get("/briefing/morning")
async def morning_briefing() -> EventSourceResponse:
    """晨报（SSE 流式，支持 Redis 缓存）"""
    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "生成今日晨报"}],
        "session_id": "briefing_morning",
        "user_id": None,
        "favorites": [],
        "intent": "morning",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for event in morning_agent.stream(state):
                yield {"data": json.dumps(event, ensure_ascii=False)}
        except Exception as e:
            yield {"data": json.dumps(
                {"type": SSEEventType.ERROR, "message": str(e)},
                ensure_ascii=False,
            )}

    return EventSourceResponse(generator())


@router.get("/briefing/alert")
async def alert_briefing(
    symbol: str,
    cycle: str = "",
) -> EventSourceResponse:
    """异动提醒（SSE 流式）

    参数：
        symbol: 6位股票代码（必填），如 600519
        cycle: 周期筛选（选填），short=短线 / mid=中线 / long=长线
    """
    from aistock_agent.agents.workers import alert as alert_agent

    state: dict[str, object] = {
        "messages": [{"role": "user", "content": f"分析 {symbol} 的异动情况"}],
        "session_id": f"briefing_alert_{symbol}",
        "user_id": None,
        "favorites": [],
        "intent": "alert",
        "symbol": symbol,
        "tag_code": cycle,
        "analysis_reports": {},
        "final_response": None,
    }

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        try:
            async for event in alert_agent.stream(state):
                yield {"data": json.dumps(event, ensure_ascii=False)}
        except Exception as e:
            yield {"data": json.dumps(
                {"type": SSEEventType.ERROR, "message": str(e)},
                ensure_ascii=False,
            )}

    return EventSourceResponse(generator())


@router.get("/skills")
async def list_skills() -> dict[str, list[dict[str, str]]]:
    """已注册工具列表

    工具通过 ``register()`` 自动注册到 Registry，本端点直接从
    ``get_exposed_skills()`` 读取。无需手动维护 ``all_tools`` 列表。
    """
    from aistock_agent.tools.registry import get_exposed_skills

    exposed_tools = get_exposed_skills()

    return {
        "tools": [
            {"name": t.name, "description": t.description}
            for t in exposed_tools
        ]
    }


# ── 健康检查 ──────────────────────────────────────────────────────


@health_router.get("/health")
async def liveness() -> dict[str, str]:
    """Liveness probe — 进程存活即返回 200（K8s livenessProbe 用）。

    不检查任何依赖：liveness 只回答"进程是否活着"，依赖连通性由
    ``/health/ready``（readinessProbe）负责。这样依赖抖动不会导致
    K8s 重启一个本来健康的进程。
    """
    return {"status": "ok"}


@health_router.get("/health/ready")
async def readiness(response: Response) -> dict[str, object]:
    """Readiness probe — 检查 Redis / Node.js API / LLM 连通性。

    - redis：``RedisPool.get_client().ping()``
    - node_api：``HttpClientPool.get_client().get("{node_api_base_url}/internal/health")``
    - llm：可选，env ``HEALTH_CHECK_LLM=true`` 时才探测（默认跳过，避免每次探针消耗 token）

    任一启用的检查失败 → 503 + ``status=degraded``；全部 ok → 200 + ``status=ok``。
    "skipped" 的检查项不计入失败（LLM 默认 skipped）。
    """
    checks: dict[str, str] = {}

    # Redis
    try:
        redis_client = await RedisPool.get_client()
        await redis_client.ping()
        checks["redis"] = "ok"
    except Exception as e:
        _health_logger.warning("health_check_redis_failed", error=str(e))
        checks["redis"] = f"error: {e}"

    # Node.js API
    try:
        http_client = await HttpClientPool.get_client()
        resp = await http_client.get(f"{settings.node_api_base_url}/internal/health")
        resp.raise_for_status()
        checks["node_api"] = "ok"
    except Exception as e:
        _health_logger.warning("health_check_node_api_failed", error=str(e))
        checks["node_api"] = f"error: {e}"

    # LLM（可选，默认跳过——避免 readiness 探针频繁消耗 token）
    if settings.health_check_llm:
        try:
            # 惰性导入：未启用时不加载 langchain_openai，保持 /health/ready 轻量
            from aistock_agent.services.llm import get_quick_think
            await get_quick_think().ainvoke("ping")
            checks["llm"] = "ok"
        except Exception as e:
            _health_logger.warning("health_check_llm_failed", error=str(e))
            checks["llm"] = f"error: {e}"
    else:
        checks["llm"] = "skipped"

    # "skipped" 不算失败，只有非 ok/非 skipped 的检查项才判定 degraded
    degraded = any(v not in ("ok", "skipped") for v in checks.values())
    if degraded:
        response.status_code = 503
        return {"status": "degraded", "checks": checks}
    return {"status": "ok", "checks": checks}
