"""REST 接口 — 对话消息、晨报、工具列表、健康检查"""

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from datetime import date
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


@router.post("/briefing/morning/trigger")
async def trigger_morning_briefing(
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发晨报生成（非流式，供管理员 curl 触发）

    直接调用 morning_agent.run()，不走 graph SSE 流。
    晨报完成后自动提取 major_events 并行执行事件传导分析，等待全部完成。
    返回 JSON 含 success / message / report_date / cached / 事件统计。
    管理员触发后可通过 ``pm2 log aistock-app-api --lines 50`` 查看 Node.js 日志。
    """
    from aistock_agent.agents.workers import morning as morning_agent
    from aistock_agent.services.event_conduction import run_event_conduction_batch

    today = date.today().isoformat()
    logger = structlog.get_logger()
    logger.info("manual_trigger_morning_start", report_date=today)

    start = time.time()

    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "生成今日晨报"}],
        "session_id": f"trigger_morning_{today}",
        "user_id": None,
        "favorites": [],
        "intent": "morning",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "manual",
    }

    try:
        result = await morning_agent.run(state)
        analysis_reports = result.get("analysis_reports", {})
        if not isinstance(analysis_reports, dict):
            analysis_reports = {}

        # 显式状态字段，禁止用 bool(final_response) 推断
        morning_generated = bool(analysis_reports.get("morning_generated", False))
        cached = bool(analysis_reports.get("cached", False))
        morning_persisted = bool(analysis_reports.get("morning_persisted", False))
        major_events = analysis_reports.get("major_events", [])
        if not isinstance(major_events, list):
            major_events = []
        has_major_events = bool(major_events)

        # 事件传导统计
        major_event_count = len(major_events)
        event_triggered_count = 0
        event_succeeded_count = 0
        event_failed_count = 0
        event_persisted_count = 0
        event_persist_failed_count = 0

        # 晨报成功生成（非降级）且有重大事件 → 触发事件传导
        if morning_generated and has_major_events:
            event_results = await run_event_conduction_batch(major_events)
            event_triggered_count = len(event_results)
            event_succeeded_count = sum(1 for r in event_results if r.event_generated)
            event_failed_count = event_triggered_count - event_succeeded_count
            # 只统计生成成功的事件的持久化状态
            event_persisted_count = sum(
                1 for r in event_results if r.event_generated and r.persisted
            )
            event_persist_failed_count = event_succeeded_count - event_persisted_count

        elapsed = time.time() - start

        logger.info(
            "manual_trigger_morning_done",
            morning_generated=morning_generated,
            cached=cached,
            morning_persisted=morning_persisted,
            has_major_events=has_major_events,
            major_event_count=major_event_count,
            event_triggered=event_triggered_count,
            event_succeeded=event_succeeded_count,
            event_failed=event_failed_count,
            event_persisted=event_persisted_count,
            event_persist_failed=event_persist_failed_count,
        )

        return {
            "success": morning_generated,
            "message": "晨报生成完成" if morning_generated else "晨报生成失败（降级）",
            "report_date": today,
            "cached": cached,
            "morning_generated": morning_generated,
            "morning_persisted": morning_persisted,
            "has_major_events": has_major_events,
            "major_event_count": major_event_count,
            "event_triggered_count": event_triggered_count,
            "event_succeeded_count": event_succeeded_count,
            "event_failed_count": event_failed_count,
            "event_persisted_count": event_persisted_count,
            "event_persist_failed_count": event_persist_failed_count,
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as e:
        logger.error("manual_trigger_morning_failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "message": f"晨报生成失败: {str(e)}",
            "report_date": today,
            "morning_generated": False,
            "cached": False,
            "morning_persisted": False,
            "has_major_events": False,
            "major_event_count": 0,
            "event_triggered_count": 0,
            "event_succeeded_count": 0,
            "event_failed_count": 0,
            "event_persisted_count": 0,
            "event_persist_failed_count": 0,
            "elapsed_seconds": round(time.time() - start, 2),
        }


@router.post("/briefing/event/trigger")
async def trigger_event_briefing(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发事件传导分析（非流式，供管理员 curl 触发）

    复用 ``run_single_event_conduction()``，与 scheduler 共享同一执行路径。
    返回 JSON 含 success / message / event_id / event_generated / event_persisted / event_cached。
    禁止硬编码 success=True，只读取显式状态。
    """
    from aistock_agent.services.event_conduction import run_single_event_conduction

    logger = structlog.get_logger()

    # 构建事件 dict：优先用请求体的 event_title，否则用默认事件描述
    event_title = (body or {}).get("event_title", "").strip() if body else ""
    event_summary = (body or {}).get("event_summary", "").strip() if body else ""
    event_url = (body or {}).get("event_url", "").strip() if body else ""

    if event_title:
        event_dict: dict[str, object] = {
            "title": event_title,
            "summary": event_summary,
            "url": event_url,
        }
    else:
        # 无 title 时构造非空默认事件，实际调用 run_single_event_conduction()
        # （恢复上一版本"分析最新重大市场事件"的行为，不因空标题提前失败）
        event_dict = {
            "title": "最新重大市场事件",
            "summary": "请分析最新的重大市场事件",
            "url": "",
        }

    logger.info("manual_trigger_event_start", event_title=event_title[:50] or "default")

    try:
        result = await run_single_event_conduction(event_dict)

        logger.info(
            "manual_trigger_event_done",
            event_title=event_title[:50] or "default",
            event_generated=result.event_generated,
            persisted=result.persisted,
            cached=result.cached,
            success=result.success,
        )

        if result.success:
            return {
                "success": True,
                "message": "事件分析完成",
                "event_id": result.event_id,
                "event_generated": result.event_generated,
                "event_persisted": result.persisted,
                # 从 event_agent 显式状态读取，禁止硬编码 False
                "event_cached": result.cached,
            }
        else:
            return {
                "success": False,
                "message": f"事件分析失败: {result.error or '未知错误'}",
                "event_id": result.event_id,
                "event_generated": result.event_generated,
                "event_persisted": result.persisted,
                "event_cached": result.cached,
            }
    except Exception as e:
        logger.error(
            "manual_trigger_event_failed",
            event_title=event_title[:50] or "default",
            error=str(e),
            exc_info=True,
        )
        return {
            "success": False,
            "message": f"事件分析失败: {str(e)}",
            "event_id": "",
            "event_generated": False,
            "event_persisted": False,
            "event_cached": False,
        }


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
        "trigger_source": "user",  # 标记用户请求来源
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


# ── 今日 AI 分析报告 ──────────────────────────────────────────────


@router.get("/reports/list")
async def list_reports(date: str = "") -> dict[str, object]:
    """今日可用的 AI 分析报告列表

    返回格式: { "date": "2026-07-15", "items": [{report_type, label, icon}, ...] }
    """
    from datetime import date as dt

    from aistock_agent.services.report_cache import list_reports as cache_list

    report_date = date or dt.today().isoformat()
    items = cache_list(report_date)
    return {"date": report_date, "items": items}


@router.get("/report/{report_type}/{report_date}")
async def get_report(report_type: str, report_date: str) -> dict[str, object]:
    """获取单个已缓存的分析报告

    URL 参数：
        report_type: morning / wind_leader / hot_burst / alert / broadcast / review
        report_date: YYYY-MM-DD
    """
    from aistock_agent.services.report_cache import get_report as cache_get

    r = cache_get(report_type, report_date)
    if r:
        return {"code": 200, "data": r}
    return {"code": 404, "message": "报告未生成", "data": None}


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
