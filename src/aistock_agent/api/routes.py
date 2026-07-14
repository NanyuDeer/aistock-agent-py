"""REST 接口 — 对话消息、晨报、工具列表、健康检查"""

import asyncio
import json
from collections.abc import AsyncGenerator
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, Response
from langgraph.graph.state import CompiledStateGraph
from sse_starlette.sse import EventSourceResponse

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
    """对话消息（非流式）

    @deprecated: use POST /chat/stream/messages instead.
    保留兼容，前端全部切到双流后清理。
    """
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


# ── session_id → asyncio.Queue 双队列扇出 ──
# messages 流和 updates 流各自拥有独立 Queue，graph 只跑一次。
# 每个事件同时推入两个 Queue，两条流独立消费、互不竞争。
_message_queues: dict[str, asyncio.Queue[object | None]] = {}
_update_queues: dict[str, asyncio.Queue[object | None]] = {}


def _ensure_message_queue(session_id: str) -> tuple[asyncio.Queue[object | None], bool]:
    """获取或创建 message 队列，同时确保 update 队列也已创建。

    graph 执行前两个队列必须同时存在，否则 _run_graph_to_queue 推入
    update 队列的事件会丢失。故首次创建 message 队列时同步创建 update 队列。

    Returns:
        (queue, is_new): message 队列 + 是否为新创建（首次调用时为 True，
        后续 messages 重连或 updates 连接时为 False）。
    """
    is_new = session_id not in _message_queues
    if is_new:
        _message_queues[session_id] = asyncio.Queue()
        _update_queues.setdefault(session_id, asyncio.Queue())
    return _message_queues[session_id], is_new


def _ensure_update_queue(session_id: str) -> asyncio.Queue[object | None]:
    """获取或创建 update 队列（不触发 graph 执行）。"""
    if session_id not in _update_queues:
        _update_queues[session_id] = asyncio.Queue()
    return _update_queues[session_id]


async def _run_graph_to_queue(
    graph: CompiledStateGraph,
    initial_state: dict[str, object],
    session_id: str,
) -> None:
    """后台执行 graph，所有 ``astream_events`` 事件推入两个独立 Queue（fan-out）。

    每个事件同时放入 message queue 和 update queue，
    两条流各自独立消费、互不竞争。仅由 messages generator 的首次连接触发。
    """
    msg_q, _ = _ensure_message_queue(session_id)
    upd_q = _ensure_update_queue(session_id)
    try:
        async for event in graph.astream_events(
            initial_state, version="v2",
            config={"configurable": {"thread_id": session_id}},
        ):
            await msg_q.put(event)
            await upd_q.put(event)
    except Exception as exc:
        error_event = {"__error__": str(exc)}
        await msg_q.put(error_event)
        await upd_q.put(error_event)
    finally:
        await msg_q.put(None)  # 哨兵：事件流结束
        await upd_q.put(None)
        _message_queues.pop(session_id, None)
        _update_queues.pop(session_id, None)


async def _stream_messages(
    graph: CompiledStateGraph,
    initial_state: dict[str, object],
    session_id: str,
) -> AsyncGenerator[dict[str, object], None]:
    """messages 流 — 从 message Queue 读事件，发射 TEXT + LLM_START + DONE"""
    queue, is_new = _ensure_message_queue(session_id)

    # 首次 messages 连接触发 graph 执行，updates 连接只读
    if is_new:
        asyncio.create_task(_run_graph_to_queue(graph, initial_state, session_id))

    _llm_started = False
    try:
        while True:
            event = await queue.get()
            if event is None:
                # graph 结束 → 取后处理结果
                final_state = await graph.aget_state(
                    config={"configurable": {"thread_id": session_id}}
                )
                yield {
                    "type": SSEEventType.DONE,
                    "final_response": final_state.values.get("final_response", ""),
                    "analysis_reports": final_state.values.get("analysis_reports", {}),
                }
                break
            if not isinstance(event, dict):
                continue
            if "__error__" in event:
                yield {"type": SSEEventType.ERROR, "message": event["__error__"]}
                break

            metadata = event.get("metadata", {})
            node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
            if node == "supervisor":
                continue

            sse = map_langgraph_event_to_sse(event, filter_type="text")
            if sse is None:
                continue
            if sse["type"] == SSEEventType.TEXT:
                if not _llm_started:
                    _llm_started = True
                    yield {"type": SSEEventType.LLM_START, "label": "正在生成回复"}
                yield sse
    except Exception as exc:
        yield {"type": SSEEventType.ERROR, "message": str(exc)}


async def _stream_updates(
    session_id: str,
) -> AsyncGenerator[dict[str, object], None]:
    """updates 流 — 从 update Queue 读事件，发射 TOOL_START/END + AGENT_SWITCH + DONE"""
    queue = _ensure_update_queue(session_id)
    _prev_node: str | None = None

    try:
        while True:
            event = await queue.get()
            if event is None:
                yield {"type": SSEEventType.DONE}
                break
            if not isinstance(event, dict):
                continue
            if "__error__" in event:
                yield {"type": SSEEventType.ERROR, "message": event["__error__"]}
                break

            metadata = event.get("metadata", {})
            node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
            if node and node != _prev_node:
                yield {
                    "type": SSEEventType.AGENT_SWITCH,
                    "from_node": _prev_node,
                    "to_node": node,
                }
                _prev_node = node

            sse = map_langgraph_event_to_sse(event, filter_type="tool")
            if sse is not None:
                yield sse
    except Exception as exc:
        yield {"type": SSEEventType.ERROR, "message": str(exc)}


# ── 路由 ──────────────────────────────────────────────────────────


@router.post("/chat/stream/messages")
async def chat_stream_messages(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> EventSourceResponse:
    """对话消息（SSE 流式，仅文本）— 逐 token 打字

    与 ``/chat/stream/updates`` 共享同一次 graph 执行（asyncio.Queue 扇出）。
    yields: llm_start → text/text/... → done（带 final_response + analysis_reports）
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
        async for sse_event in _stream_messages(graph, initial_state, session_id):
            yield {"data": json.dumps(sse_event, ensure_ascii=False)}

    return EventSourceResponse(generator())


@router.post("/chat/stream/updates")
async def chat_stream_updates(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> EventSourceResponse:
    """对话进度（SSE 流式，仅工具事件）— 侧边栏/状态栏

    与 ``/chat/stream/messages`` 共享同一次 graph 执行（双 Queue fan-out）。
    updates 流仅从 _update_queues 读取，不启动 graph 执行、不编译 graph。
    yields: agent_switch → tool_start/tool_end/... → done
    """
    session_id = req.session_id or f"session_{id(req)}"

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        async for sse_event in _stream_updates(session_id):
            yield {"data": json.dumps(sse_event, ensure_ascii=False)}

    return EventSourceResponse(generator())


@router.get("/briefing/morning")
async def morning_briefing() -> EventSourceResponse:
    """晨报（SSE 流式，走 graph 转发）

    复用 ``_stream_messages`` generator，不再调用 morning.stream()。
    缓存命中时 supervisor 花 ~0.5s 分类后立即返回；缓存未命中时逐 token 流式。
    """
    graph = compile_graph()
    session_id = f"briefing_morning_{uuid4().hex[:8]}"

    initial_state = build_initial_state(
        message="生成今日晨报",
        session_id=session_id,
        user_id=None,
        favorites=[],
    )

    async def generator() -> AsyncGenerator[dict[str, str], None]:
        async for sse_event in _stream_messages(graph, initial_state, session_id):
            yield {"data": json.dumps(sse_event, ensure_ascii=False)}

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
