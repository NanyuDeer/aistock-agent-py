"""REST 接口 — 对话消息、晨报、工具列表、健康检查"""

import asyncio
import json
import re
import time
from collections.abc import AsyncGenerator
from datetime import date
from uuid import uuid4

import structlog
from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from langgraph.graph.state import CompiledStateGraph
from sse_starlette.sse import EventSourceResponse

from aistock_agent.api.deps import (
    build_chat_initial_state,
    build_initial_state,
    reset_transient_state,
    verify_internal_token,
)
from aistock_agent.config import settings
from aistock_agent.constants import CHAT_NODE_LABELS, SSEEventType
from aistock_agent.graph.builder import compile_graph
from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.graph.nodes.qa_router import _STOCK_SYMBOL_CLARIFICATION
from aistock_agent.memory.checkpointer import delete_thread
from aistock_agent.observability.metrics import get_metrics_collector as _get_metrics_collector
from aistock_agent.schemas.chat import ChatRequest, ChatResponse
from aistock_agent.schemas.qa_api import QARequest
from aistock_agent.schemas.stock_trace import StockTraceTriggerRequest, StockTraceTriggerResponse
from aistock_agent.services.briefing import build_and_persist_brief
from aistock_agent.services.data_client import node_api
from aistock_agent.services.http_client import HttpClientPool
from aistock_agent.services.qa_briefing import (
    QaBriefingPrerequisiteError,
    QaBriefingRunError,
    run_qa_brief_chain,
)
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.services.token_usage import reset_token_usage
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.utils.date import shanghai_today
from aistock_agent.utils.sse import map_langgraph_event_to_sse


def _select_graph() -> CompiledStateGraph:
    """按入口路由选择 graph：Chat 入口（/chat/*、/ws/chat）恒走 ChatAgent。

    报告入口（/briefing/*、trigger 类）不经过本函数，直接使用 compile_graph()。
    ``chat_graph_enabled`` 字段保留在 config 中作回滚闸门，路由不再读取
    （回滚时临时恢复读取该字段即可）。
    """
    return compile_chat_graph()


router = APIRouter()

# 健康检查路由（在 main.py 挂载到根路径，不在 /api/agent 前缀下）
health_router = APIRouter(tags=["health"])
_health_logger = structlog.get_logger()
_metrics = _get_metrics_collector()
_qa_logger = structlog.get_logger()

# session_id 格式与 app-api SESSION_ID_RE 对齐：仅字母/数字/_/-，长度 1-64
_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_chat_logger = structlog.get_logger()


def _resolve_manual_report_date(body: dict[str, str] | None) -> str:
    """缺省使用上海当天；显式日期必须是有效的 YYYY-MM-DD。"""
    if not body or "report_date" not in body:
        return shanghai_today().isoformat()

    report_date = body["report_date"]
    try:
        parsed_date = date.fromisoformat(report_date)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="report_date 必须是有效的 YYYY-MM-DD") from exc
    if parsed_date.isoformat() != report_date:
        raise HTTPException(status_code=422, detail="report_date 必须是有效的 YYYY-MM-DD")
    return report_date


def _validate_scrape_date(date_str: str) -> str:
    """校验事件抓取查询日期（YYYY-MM-DD），非法返回 400 结构化错误。

    三层校验（对齐 Node 侧 /internal/analysis-reports 的日期校验惯例）：
    正则格式 → fromisoformat 语义 → isoformat 回写一致性。非法格式与
    语义非法日期（如 2026-13-45）均返回 400，避免把坏日期透传到事件库。
    """
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date_str):
        raise HTTPException(status_code=400, detail="date 必须是有效的 YYYY-MM-DD")
    try:
        parsed_date = date.fromisoformat(date_str)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="date 必须是有效的 YYYY-MM-DD") from exc
    if parsed_date.isoformat() != date_str:
        raise HTTPException(status_code=400, detail="date 必须是有效的 YYYY-MM-DD")
    return date_str


def _resolve_qa_report_date(body: dict[str, str] | None) -> str:
    """QA runner 必须显式指定固定上海日期，禁止回退到当天。"""
    if not body or "report_date" not in body:
        raise HTTPException(status_code=422, detail="QA runner 必须提供 report_date")
    return _resolve_manual_report_date(body)


@router.post("/chat/message", response_model=ChatResponse)
async def chat_message(
    req: ChatRequest,
    _: None = Depends(verify_internal_token),
) -> ChatResponse:
    """对话消息（非流式）

    @deprecated: use POST /chat/stream/messages instead.
    保留兼容，前端全部切到双流后清理。
    """
    graph = _select_graph()
    reset_token_usage()  # P10 线 2：HTTP 非流式路径按轮重置（一致性；不落库，仅透出展示）
    session_id = req.session_id or f"session_{id(req)}"
    initial_state = build_chat_initial_state(req.message)
    user_id_value = req.user_id or None  # D11：HTTP 降级路径透传（对齐 WS）
    initial_state["user_id"] = user_id_value
    # Phase 4-3（改进 15）：无条件注入 user_profile（对齐 ws.py；匿名显式 None
    # 覆盖 checkpoint 旧值防跨轮污染；拉取失败 None 不阻断）
    initial_state["user_profile"] = (
        await node_api.get_user_profile(user_id_value) if user_id_value else None
    )
    initial_state["force_deep"] = req.force_deep     # D4：HTTP 降级路径透传（对齐 ws.py）
    reset_transient_state(initial_state)  # M3：单轮 transient 每轮归零（对齐 ws.py）
    # G1 修订（2026-08-17 design-debate 定案）：值源 = 末节点 synth_answer 输出
    result: dict[str, object] = {}
    async for step in graph.astream(
        initial_state,
        config={"configurable": {"thread_id": session_id}},
        stream_mode="updates",
    ):
        if isinstance(step, dict) and "synth_answer" in step:
            result = step["synth_answer"]  # 终节点，最后一次命中即本轮输出
    # 澄清分支显式置 None
    if not result.get("final_response") and result.get("confirm"):
        return ChatResponse(
            content=_STOCK_SYMBOL_CLARIFICATION,
            session_id=session_id,
            token_usage=result.get("token_usage"),
            last_deep_report=None,
            cards=None,
        )
    content = result.get("final_response") or "抱歉，我暂时无法处理您的请求。"
    return ChatResponse(
        content=content,
        session_id=session_id,
        token_usage=result.get("token_usage"),
        last_deep_report=result.get("last_deep_report"),
        cards=(
            [c.model_dump() for c in result["cards"]]
            if result.get("cards")
            else None
        ),
    )


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
    # G1 修订（2026-08-17）：last_deep_report/cards 改为事件流采集
    round_last_deep_report: dict[str, object] | None = None
    round_cards: list[dict[str, object]] | None = None
    try:
        while True:
            event = await queue.get()
            if event is None:
                # graph 结束 → 取后处理结果
                final_state = await graph.aget_state(
                    config={"configurable": {"thread_id": session_id}}
                )
                # Phase 4-2（改进 13）：SSE 与 HTTP 同样无两阶段交互能力；qa_router
                # 触发 confirm 时降级为既有澄清文本（等价 WS confirm_timeout 回退）。
                final_response = final_state.values.get("final_response", "")
                if not final_response and final_state.values.get("confirm"):
                    final_response = _STOCK_SYMBOL_CLARIFICATION
                yield {
                    "type": SSEEventType.DONE,
                    "final_response": final_response,
                    "analysis_reports": final_state.values.get("analysis_reports", {}),
                    # P10 线 2：SSE 降级路径同步附带（无则 None，null 兼容；
                    # 仅供前端本地累加展示，本路径不落库）
                    "token_usage": final_state.values.get("token_usage"),
                    # G1 修订：DONE 补齐 last_deep_report（事件流采集值，非终态）
                    "last_deep_report": round_last_deep_report,
                    "cards": round_cards,
                }
                break
            if not isinstance(event, dict):
                continue
            if "__error__" in event:
                yield {"type": SSEEventType.ERROR, "message": event["__error__"]}
                break

            # G1 修订：采集 synth_answer 节点输出
            if event.get("event") == "on_chain_end" and event.get("name") == "synth_answer":
                output = event.get("data", {}).get("output")
                if isinstance(output, dict):
                    round_last_deep_report = output.get("last_deep_report")
                    cards = output.get("cards")
                    round_cards = [c.model_dump() for c in cards] if cards else None

            metadata = event.get("metadata", {})
            node = metadata.get("langgraph_node") if isinstance(metadata, dict) else None
            if node in ("supervisor", "qa_router"):
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
                agent_switch: dict[str, object] = {
                    "type": SSEEventType.AGENT_SWITCH,
                    "from_node": _prev_node,
                    "to_node": node,
                }
                if node in CHAT_NODE_LABELS:
                    agent_switch["label"] = CHAT_NODE_LABELS[node]
                yield agent_switch
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
    graph = _select_graph()
    reset_token_usage()  # P10 线 2：SSE 路径按轮重置（DONE 从 state.values 读快照）
    session_id = req.session_id or f"session_{id(req)}"
    initial_state = build_chat_initial_state(req.message)
    user_id_value = req.user_id or None  # D11：HTTP 降级路径透传（对齐 WS）
    initial_state["user_id"] = user_id_value
    # Phase 4-3（改进 15）：SSE 路径同样无条件注入 user_profile（对齐 ws.py；
    # 匿名显式 None 覆盖 checkpoint 旧值防跨轮污染）
    initial_state["user_profile"] = (
        await node_api.get_user_profile(user_id_value) if user_id_value else None
    )
    initial_state["force_deep"] = req.force_deep     # D4：HTTP 降级路径透传（对齐 ws.py）
    reset_transient_state(initial_state)  # M3：单轮 transient 每轮归零（对齐 ws.py）

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


# ── 会话 thread 管理（Phase 5：删会话联动删 checkpointer thread） ──


@router.delete("/internal/chat/threads/{session_id}")
async def delete_chat_thread(
    session_id: str,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """删除 chat checkpointer thread（幂等；Phase 5 删会话联动删历史）。

    app-api 在删除 chat_sessions 元数据成功后调用本端点，清理 sqlite
    .langgraph.db 中按 thread_id（= session_id）存储的多轮检查点，避免
    thread 成为孤儿、以及 session_id 复用导致的历史串扰。
    - 鉴权：X-Internal-Token 缺失/不匹配 → 403；
    - session_id 非法（非 [A-Za-z0-9_-]{1,64}）→ 400；
    - thread 不存在 → 仍返回 200（幂等）；
    - delete_thread 意外异常 → 500（"永不 500" 由 app-api 调用侧保证）。
    """
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(
            status_code=400,
            detail="session_id 仅支持字母/数字/_/-，长度 1-64",
        )
    try:
        delete_thread(session_id)
    except Exception as exc:
        _chat_logger.warning(
            "delete_chat_thread_failed", session_id=session_id, error=str(exc)
        )
        raise HTTPException(status_code=500, detail="chat thread delete failed") from exc
    return {"success": True, "message": "chat thread deleted"}


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
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发晨报生成（非流式，供管理员 curl 触发）

    直接调用 morning_agent.run()，不走 graph SSE 流。
    返回 JSON 含 success / message / report_date / cached / 事件统计。
    管理员触发后可通过 ``pm2 log aistock-app-api --lines 50`` 查看 Node.js 日志。

    事件传导：2026-08-12（Task 5）起传导触发统一由事件抓取中台负责
    （event_scrape 入库成功后触发 run_event_analysis_pipeline），本入口不再直接触发
    event conduction——否则对同批事件（07:30 全量/每小时增量已触发）双跑
    （Task 4 评审 M2）。响应中的 event_*_count 字段保留并恒为 0（接口契约，
    Node 侧 morning_trigger_handler 消费）。
    """
    from aistock_agent.agents.workers import morning as morning_agent

    report_date = _resolve_manual_report_date(body)
    logger = structlog.get_logger()
    logger.info("manual_trigger_morning_start", report_date=report_date)

    start = time.time()

    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "生成今日晨报"}],
        "session_id": f"trigger_morning_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": "morning",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "scheduler",
        "report_date": report_date,
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

        # 事件传导统计：传导统一由中台负责（Task 5），本入口不再触发，
        # 字段保留恒 0（响应契约：Node 侧 morning_trigger_handler.ts 消费
        # event_triggered_count / event_succeeded_count / event_failed_count /
        # event_persisted_count / event_persist_failed_count）。
        major_event_count = len(major_events)
        event_triggered_count = 0
        event_succeeded_count = 0
        event_failed_count = 0
        event_persisted_count = 0
        event_persist_failed_count = 0

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
            "report_date": report_date,
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
            "report_date": report_date,
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
            event_generated=result.status.event_generated,
            persisted=result.status.persisted,
            cached=result.status.cached,
            success=result.status.success,
        )

        if result.status.success:
            return {
                "success": True,
                "message": "事件分析完成",
                "event_id": result.status.event_id,
                "event_generated": result.status.event_generated,
                "event_persisted": result.status.persisted,
                # 从 event_agent 显式状态读取，禁止硬编码 False
                "event_cached": result.status.cached,
            }
        else:
            return {
                "success": False,
                "message": f"事件分析失败: {result.status.error or '未知错误'}",
                "event_id": result.status.event_id,
                "event_generated": result.status.event_generated,
                "event_persisted": result.status.persisted,
                "event_cached": result.status.cached,
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


@router.post("/briefing/event-scrape/trigger")
async def trigger_event_scrape(
    body: dict[str, object] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发统一事件抓取。

    body: {"scrape_mode": "full_daily|intraday|event_triggered",
           "score_date": "YYYY-MM-DD", "event": {...}}

    返回契约（对齐既有 trigger 接口 success/message 风格）：
    - 成功: {"success": True, "data": <run_event_scrape 结果>}
    - 失败: {"success": False, "message": <错误说明>}
      非法 scrape_mode 与 run_event_scrape 异常均走结构化错误体，不抛 500。
    """
    from aistock_agent.services.event_scraper import VALID_MODES, run_event_scrape

    logger = structlog.get_logger()
    payload = body or {}
    scrape_mode = str(payload.get("scrape_mode", "full_daily"))
    # allowlist 校验：非法值返回结构化错误，避免 run_event_scrape 抛 ValueError → 500
    if scrape_mode not in VALID_MODES:
        logger.warning(
            "manual_trigger_event_scrape_invalid_mode",
            scrape_mode=scrape_mode,
            valid_modes=sorted(VALID_MODES),
        )
        return {
            "success": False,
            "message": f"未知 scrape_mode: {scrape_mode!r}，合法值: {sorted(VALID_MODES)}",
        }
    score_date = payload.get("score_date")
    event = payload.get("event")
    logger.info("manual_trigger_event_scrape_start", scrape_mode=scrape_mode)
    try:
        result = await run_event_scrape(
            scrape_mode,
            score_date=str(score_date) if score_date else None,
            event=dict(event) if isinstance(event, dict) else None,
        )
        logger.info("manual_trigger_event_scrape_done", **result)
        return {"success": True, "data": result}
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "manual_trigger_event_scrape_failed",
            scrape_mode=scrape_mode,
            error=str(exc),
            exc_info=True,
        )
        return {"success": False, "message": f"事件抓取失败: {str(exc)}"}


@router.get("/event/scrape-list")
async def event_scrape_list(date: str) -> dict[str, object]:
    """按日期读取当日抓取事件列表（事件抓取中台查询接口）。

    URL 参数：
        date: YYYY-MM-DD（必填）。非法格式/语义非法日期返回 400。
    返回: {"events": [EventRecord, ...]}；当日无抓取事件返回空列表。
    """
    from aistock_agent.services.event_store import load_event_scrape  # noqa: PLC0415

    date = _validate_scrape_date(date)
    events = await load_event_scrape(date)
    return {"events": events}


@router.get("/event/scrape-by-symbol/{symbol}")
async def event_scrape_by_symbol(symbol: str, date: str) -> dict[str, object]:
    """按标的读取当日抓取事件（stock_trace 证据源用）。

    URL 参数：
        symbol: 6 位股票代码（必填）。
        date: YYYY-MM-DD（必填）。非法格式/语义非法日期返回 400。
    返回: {"events": [EventRecord, ...]}；按 payload.symbol / involved_keywords
    子串过滤（"000" 类短符号可能误命中多股，对 stock_trace 证据源可接受，
    Task 2 评审备注）。
    """
    from aistock_agent.services.event_store import (  # noqa: PLC0415
        load_event_scrape_by_symbol,
    )

    date = _validate_scrape_date(date)
    events = await load_event_scrape_by_symbol(symbol, date)
    return {"events": events}


@router.post("/briefing/review/trigger")
async def trigger_review_briefing(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发复盘溯源生成（非流式，供管理员 curl 触发）

    直接调用 review_agent.run()，不走 graph SSE 流。
    返回 JSON 含 success / message / report_date / cached / elapsed_seconds。
    管理员触发后可通过 ``pm2 log aistock-agent-py --lines 50`` 查看 Python 日志。
    """
    from aistock_agent.agents.workers import review as review_agent

    # 支持指定历史日期，默认上海当天
    report_date = _resolve_manual_report_date(body)
    logger = structlog.get_logger()
    logger.info("manual_trigger_review_start", report_date=report_date)

    start = time.time()

    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "生成今日复盘溯源"}],
        "session_id": f"trigger_review_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": "review",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "scheduler",
        "report_date": report_date,
    }

    try:
        result = await review_agent.run(state)
        final_response = result.get("final_response")
        generated = (
            isinstance(final_response, str)
            and final_response != "收盘溯源生成暂时不可用，请稍后重试"
        )
        elapsed = time.time() - start

        logger.info(
            "manual_trigger_review_done",
            generated=generated,
            elapsed_seconds=round(elapsed, 2),
        )

        return {
            "success": generated,
            "message": "复盘溯源生成完成" if generated else "复盘溯源生成失败（降级）",
            "report_date": report_date,
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as e:
        logger.error("manual_trigger_review_failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "message": f"复盘溯源生成失败: {str(e)}",
            "report_date": report_date,
            "elapsed_seconds": round(time.time() - start, 2),
        }


@router.post("/briefing/broadcast/trigger")
async def trigger_broadcast_chain(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发完整播报链路（非流式，供管理员 curl 触发）

    串行执行：morning → wind_leader → hot_burst → trend_score → broadcast。
    绕过 is_trading_day() 检查，非交易日也可手动测试。
    每个 Agent 异常独立捕获，不影响后续 Agent 执行。
    返回 JSON 含整体 success / message / 各步骤状态 / elapsed_seconds。

    管理员触发后可通过 ``pm2 log aistock-agent-py --lines 100`` 查看 Python 日志。
    """
    from aistock_agent.agents.workers import broadcast as broadcast_agent
    from aistock_agent.agents.workers import hot_burst as hot_burst_agent
    from aistock_agent.agents.workers import morning as morning_agent
    from aistock_agent.agents.workers import trend_score as trend_score_agent
    from aistock_agent.agents.workers import wind_leader as wind_leader_agent

    # 支持指定历史日期（如 {"report_date": "2026-07-18"}），默认今天
    today = (body or {}).get("report_date", shanghai_today().isoformat())
    logger = structlog.get_logger()
    logger.info("manual_trigger_broadcast_start", report_date=today)

    start = time.time()

    def _make_state(intent: str | None = None) -> dict[str, object]:
        """构造手动触发链路的 AgentState（trigger_source=scheduler 使报告写DB，
        与 09:00 调度任务一致）"""
        return {
            "messages": [],
            "session_id": f"manual_broadcast_{today}",
            "user_id": None,
            "favorites": [],
            "intent": intent,
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
            "trigger_source": "scheduler",
            "report_date": today,
        }

    steps: list[dict[str, object]] = []

    def _record(step: str, success: bool, elapsed: float, error: str = "") -> None:
        steps.append({
            "step": step,
            "success": success,
            "elapsed_seconds": round(elapsed, 2),
            "error": error,
        })

    # Step 1: 晨报
    step_start = time.time()
    try:
        morning_result = await morning_agent.run(_make_state("morning"))
        morning_ok = bool(morning_result.get("final_response"))
        _record("morning", morning_ok, time.time() - step_start)
        logger.info("manual_trigger_broadcast_morning_done", success=morning_ok)
    except Exception as e:
        _record("morning", False, time.time() - step_start, str(e))
        logger.error("manual_trigger_broadcast_morning_failed", error=str(e), exc_info=True)

    # Step 2: 风口分析
    step_start = time.time()
    try:
        wind_result = await wind_leader_agent.run(_make_state())
        wind_ok = bool(wind_result.get("final_response"))
        _record("wind_leader", wind_ok, time.time() - step_start)
        logger.info("manual_trigger_broadcast_wind_done", success=wind_ok)
    except Exception as e:
        _record("wind_leader", False, time.time() - step_start, str(e))
        logger.error("manual_trigger_broadcast_wind_failed", error=str(e), exc_info=True)

    # Step 3: 机构调研热门股
    step_start = time.time()
    try:
        burst_result = await hot_burst_agent.run(_make_state())
        burst_ok = bool(burst_result.get("final_response"))
        _record("hot_burst", burst_ok, time.time() - step_start)
        logger.info("manual_trigger_broadcast_burst_done", success=burst_ok)
    except Exception as e:
        _record("hot_burst", False, time.time() - step_start, str(e))
        logger.error("manual_trigger_broadcast_burst_failed", error=str(e), exc_info=True)

    # Step 3.5: 趋势股评分分析（写DB供 broadcast 消费 + 前端查询）
    step_start = time.time()
    try:
        trend_result = await trend_score_agent.run(_make_state())
        trend_ok = bool(trend_result.get("final_response"))
        _record("trend_score", trend_ok, time.time() - step_start)
        logger.info("manual_trigger_broadcast_trend_done", success=trend_ok)
    except Exception as e:
        _record("trend_score", False, time.time() - step_start, str(e))
        logger.error("manual_trigger_broadcast_trend_failed", error=str(e), exc_info=True)

    # Step 4: 播报生成（从数据库读取报告）
    # 与 09:00 调度链路（scheduler._run_broadcast_task）保持一致：
    # 先聚合持久化 brief_{brief_type}，再运行 broadcast agent 生成双人播报。
    # 若缺这一步，brief_morning 报告不存在 → broadcast 报告降级
    # （has_source_brief=false）→ 前端 briefing 页 getBrief 查询 404。
    step_start = time.time()
    try:
        if not await build_and_persist_brief("morning", today):
            raise RuntimeError("brief 聚合持久化失败")
        broadcast_result = await broadcast_agent.run({**_make_state(), "brief_type": "morning"})
        broadcast_ok = bool(broadcast_result.get("final_response"))
        _record("broadcast", broadcast_ok, time.time() - step_start)
        logger.info(
            "manual_trigger_broadcast_final_done",
            success=broadcast_ok,
            has_audio=bool(broadcast_result.get("audio_path")),
        )
    except Exception as e:
        _record("broadcast", False, time.time() - step_start, str(e))
        logger.error("manual_trigger_broadcast_final_failed", error=str(e), exc_info=True)

    elapsed = time.time() - start
    succeeded = sum(1 for s in steps if s["success"])
    total = len(steps)

    logger.info(
        "manual_trigger_broadcast_done",
        succeeded=succeeded,
        total=total,
        elapsed_seconds=round(elapsed, 2),
    )

    return {
        "success": succeeded == total,
        "message": f"播报链路完成: {succeeded}/{total} 步成功",
        "report_date": today,
        "steps": steps,
        "succeeded": succeeded,
        "total": total,
        "elapsed_seconds": round(elapsed, 2),
    }


@router.post("/briefing/broadcast/only")
async def trigger_broadcast_only(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """仅重新生成双人播报（不重跑 morning/wind_leader/hot_burst/trend_score 报告）。

    broadcast_agent.run() 直接从数据库读取现有报告生成双人对话播报，
    适合"报告已存在、仅播报失败/想重听"的场景。trigger_source=scheduler 使
    播报文本写 DB + 生成双人音频（与 09:00 调度链路一致）。

    请求体: {"report_date": "2026-08-07"}（可选，默认今天）
    返回: {"success", "message", "report_date", "has_audio", "elapsed_seconds"}
    """
    from aistock_agent.agents.workers import broadcast as broadcast_agent

    today = (body or {}).get("report_date", shanghai_today().isoformat())
    logger = structlog.get_logger()
    logger.info("manual_trigger_broadcast_only_start", report_date=today)

    start = time.time()
    try:
        state: dict[str, object] = {
            "messages": [],
            "session_id": f"manual_broadcast_only_{today}",
            "user_id": None,
            "favorites": [],
            "intent": "broadcast",
            "symbol": None,
            "tag_code": None,
            "analysis_reports": {},
            "final_response": None,
            "trigger_source": "scheduler",  # 使播报写 DB + 生成音频
            "report_date": today,
        }
        result = await broadcast_agent.run(state)
        generated = bool(result.get("final_response"))
        has_audio = bool(result.get("audio_path"))
        elapsed = time.time() - start
        logger.info(
            "manual_trigger_broadcast_only_done",
            generated=generated,
            has_audio=has_audio,
            elapsed_seconds=round(elapsed, 2),
        )
        return {
            "success": generated,
            "message": (
                "播报生成完成: 文本="
                f"{'成功' if generated else '失败'} / 音频="
                f"{'已生成' if has_audio else '未生成'}"
            ),
            "report_date": today,
            "has_audio": has_audio,
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as e:
        logger.error("manual_trigger_broadcast_only_failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "message": f"播报生成失败: {str(e)}",
            "report_date": today,
            "has_audio": False,
            "elapsed_seconds": round(time.time() - start, 2),
        }


@router.post("/briefing/wind-leader/trigger")
async def trigger_wind_leader(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发风口龙头 Agent 报告生成（非流式，供管理员 curl 触发）

    直接调用 wind_leader_agent.run()，生成 AI 分析报告并写入数据库。
    trigger_source=scheduler 会先做数据预检：后端数据为空时自动调
    ``POST /api/cn/wind-leaders/refresh`` 补数据，最多重试3次。
    绕过 is_trading_day() 检查，非交易日也可手动测试。

    返回 JSON 含 success / message / report_date / has_response / elapsed_seconds。
    管理员触发后可通过 ``pm2 logs aistock-agent-py --lines 50`` 查看 Python 日志。
    """
    from aistock_agent.agents.workers import wind_leader as wind_leader_agent

    today = (body or {}).get("report_date", shanghai_today().isoformat())
    logger = structlog.get_logger()
    logger.info("manual_trigger_wind_leader_start", report_date=today)

    start = time.time()

    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "生成风口龙头分析报告"}],
        "session_id": f"manual_wind_leader_{today}",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "scheduler",
        "report_date": today,
    }

    try:
        result = await wind_leader_agent.run(state)
        final_response = result.get("final_response")
        generated = bool(final_response)
        elapsed = time.time() - start

        logger.info(
            "manual_trigger_wind_leader_done",
            generated=generated,
            elapsed_seconds=round(elapsed, 2),
        )

        return {
            "success": generated,
            "message": "风口龙头报告生成完成" if generated else "风口龙头报告生成失败（降级）",
            "report_date": today,
            "has_response": generated,
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as e:
        logger.error("manual_trigger_wind_leader_failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "message": f"风口龙头报告生成失败: {str(e)}",
            "report_date": today,
            "has_response": False,
            "elapsed_seconds": round(time.time() - start, 2),
        }


@router.post("/briefing/trend-score/trigger")
async def trigger_trend_score(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发趋势股评分 Agent 报告生成（非流式，供管理员 curl 触发）

    直接调用 trend_score_agent.run()，生成 AI 分析报告并写入数据库。
    绕过 is_trading_day() 检查，非交易日也可手动测试。
    前提：趋势股评分数据已由 Node.js TrendBatchService 计算完成（可先调
    ``POST /api/internal/trigger-trend-batch`` 补数据）。

    返回 JSON 含 success / message / report_date / elapsed_seconds。
    管理员触发后可通过 ``pm2 log aistock-agent-py --lines 50`` 查看 Python 日志。
    """
    from aistock_agent.agents.workers import trend_score as trend_score_agent

    today = (body or {}).get("report_date", shanghai_today().isoformat())
    logger = structlog.get_logger()
    logger.info("manual_trigger_trend_score_start", report_date=today)

    start = time.time()

    state: dict[str, object] = {
        "messages": [{"role": "user", "content": "生成趋势股评分分析报告"}],
        "session_id": f"manual_trend_score_{today}",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "scheduler",
        "report_date": today,
    }

    try:
        result = await trend_score_agent.run(state)
        final_response = result.get("final_response")
        generated = bool(final_response)
        elapsed = time.time() - start

        logger.info(
            "manual_trigger_trend_score_done",
            generated=generated,
            elapsed_seconds=round(elapsed, 2),
        )

        return {
            "success": generated,
            "message": "趋势股评分报告生成完成" if generated else "趋势股评分报告生成失败（降级）",
            "report_date": today,
            "has_response": generated,
            "elapsed_seconds": round(elapsed, 2),
        }
    except Exception as e:
        logger.error("manual_trigger_trend_score_failed", error=str(e), exc_info=True)
        return {
            "success": False,
            "message": f"趋势股评分报告生成失败: {str(e)}",
            "report_date": today,
            "has_response": False,
            "elapsed_seconds": round(time.time() - start, 2),
        }


@router.post("/qa/briefing/run")
async def run_qa_briefing(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """以固定日期已审核工件生成 QA Brief、播报和音频。"""
    if not settings.qa_mode_enabled:
        raise HTTPException(status_code=404, detail="QA runner 不可用")
    if not settings.qa_run_id:
        raise HTTPException(status_code=503, detail="QA_RUN_ID 未配置")

    run_id = body.get("run_id") if body else None
    if not isinstance(run_id, str) or run_id != settings.qa_run_id:
        raise HTTPException(status_code=403, detail="QA run_id 不匹配")

    brief_type = body.get("brief_type") if body else None
    if brief_type not in {"morning", "evening"}:
        raise HTTPException(status_code=422, detail="brief_type 必须是 morning 或 evening")

    report_date = _resolve_qa_report_date(body)
    try:
        return await run_qa_brief_chain(brief_type, report_date, run_id)
    except QaBriefingPrerequisiteError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except QaBriefingRunError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/trace/stock/trigger")
async def trigger_stock_trace(
    req: StockTraceTriggerRequest,
    _: None = Depends(verify_internal_token),
) -> StockTraceTriggerResponse:
    """触发个股 Trace 分析（Node StockInfoPushService → 本路由 → alert.run → 持久化）

    每窗口每 symbol 最多触发一次。
    失败返回 degraded 而非 HTTP 错误 — Node relay 据此分类日志。
    """
    from aistock_agent.agents.workers import alert as alert_agent

    _trace_logger = structlog.get_logger()

    symbol = req.symbol
    trace_id = req.trace_id or uuid4().hex
    report_date = req.report_date or shanghai_today()

    state: dict[str, object] = {
        "messages": [{"role": "user", "content": f"分析 {symbol} 的异动情况"}],
        "session_id": f"stock_trace_{trace_id}",
        "user_id": None,
        "favorites": [],
        "intent": "alert",
        "symbol": symbol,
        "tag_code": req.cycle,
        "analysis_reports": {},
        "final_response": None,
        "trigger_source": "stock_trace",
        "report_date": report_date.isoformat(),
        "trace_id": trace_id,
    }

    _rd: date | None = report_date
    if not isinstance(_rd, date):
        _rd = date.fromisoformat(str(_rd))

    try:
        result = await alert_agent.run(state)

        final_response = result.get("final_response", "")
        if not final_response:
            _trace_logger.warning("stock_trace_empty_response", symbol=symbol, trace_id=trace_id)
            return StockTraceTriggerResponse(
                trace_id=trace_id,
                symbol=symbol,
                report_date=_rd,
                status="degraded",
                degraded_reason="alert.run returned empty response",
            )

        trace_persisted = result.get("trace_persisted", False)
        report_id = result.get("report_id")

        if trace_persisted and report_id is not None:
            _trace_logger.info(
                "stock_trace_completed",
                symbol=symbol, trace_id=trace_id, report_id=report_id,
            )
            return StockTraceTriggerResponse(
                trace_id=trace_id,
                symbol=symbol,
                report_date=_rd,
                status="completed",
                report_id=report_id,
            )
        else:
            _trace_logger.warning("stock_trace_save_failed", symbol=symbol, trace_id=trace_id)
            return StockTraceTriggerResponse(
                trace_id=trace_id,
                symbol=symbol,
                report_date=_rd,
                status="degraded",
                degraded_reason="failed to persist analysis report",
            )
    except Exception as e:
        _trace_logger.error(
            "stock_trace_trigger_failed",
            symbol=symbol, trace_id=trace_id, error=str(e), exc_info=True,
        )
        return StockTraceTriggerResponse(
            trace_id=trace_id,
            symbol=symbol,
            report_date=_rd if _rd is not None else shanghai_today(),
            status="degraded",
            degraded_reason=str(e),
        )


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
    from aistock_agent.services.report_cache import list_reports as cache_list

    report_date = date or shanghai_today().isoformat()
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


# ── 观测指标 ──────────────────────────────────────────────────────


async def get_stock_trace_observability() -> dict[str, object]:
    """组装 stock_trace 观测快照：进程内计数器 + Redis 实时 gauge。

    计数器来自 MetricsCollector（stock_trace 链路，Task 2 扩展）；gauge 实时读
    Redis（stream lag / DLQ 长度 / 未确认 pending），读失败以降级值返回，不阻塞 /metrics。
    """
    from aistock_agent.observability.metrics import get_metrics
    from aistock_agent.workers.stock_trace_consumer import DLQ_STREAM, STREAM

    snapshot = get_metrics()
    stock_trace = dict(snapshot.get("stock_trace", {}))
    gauges: dict[str, object] = {"stream_lag": 0, "dlq_length": 0, "pending_unacked": 0}
    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.from_url(settings.stock_trace_redis_url)
        try:
            gauges["dlq_length"] = await redis_client.xlen(DLQ_STREAM)
            groups = await redis_client.xinfo_groups(STREAM)
            if groups:
                gauges["stream_lag"] = groups[0].get("lag", 0)
            pending = await redis_client.xpending(
                STREAM, settings.stock_trace_consumer_group
            )
            if pending:
                # 未指定 count 时 xpending 返回摘要，首元素即 pending 计数
                gauges["pending_unacked"] = pending[0]
        finally:
            await redis_client.aclose()
    except Exception:
        logger = structlog.get_logger()
        logger.warning("metrics_redis_read_failed")
    return {"stock_trace": {**stock_trace, **gauges}}


@health_router.get("/metrics")
async def metrics() -> dict[str, object]:
    """观测指标（Prometheus 语义的 JSON 快照）。含 stock_trace 计数与实时 gauge。

    注意：挂在 health_router 下，/metrics 在根路径（main.py 挂载），不带 /api/agent 前缀。
    """
    return await get_stock_trace_observability()


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

    # Stock Trace Consumer 心跳（集成模式下检查；未启用或刚启动未消费过则 skipped/pending）
    try:
        import aistock_agent.workers.stock_trace_consumer as _stc_module

        if not _stc_module._stock_trace_consumer_enabled:
            checks["stock_trace_consumer"] = "skipped"
        elif _stc_module._stock_trace_consumer_last_heartbeat is None:
            checks["stock_trace_consumer"] = "pending"
        else:
            age = time.time() - _stc_module._stock_trace_consumer_last_heartbeat
            if age > 60:
                checks["stock_trace_consumer"] = f"error: heartbeat stale ({age:.0f}s)"
            else:
                checks["stock_trace_consumer"] = f"ok ({age:.0f}s)"
    except Exception as e:
        _health_logger.warning("health_check_stock_trace_consumer_failed", error=str(e))
        checks["stock_trace_consumer"] = f"error: {e}"

    # "skipped"/"pending" 不算失败，只有非 ok/非 skipped/非 pending 的检查项才判定 degraded
    degraded = any(
        v not in ("ok", "skipped", "pending") and not v.startswith("ok ")
        for v in checks.values()
    )
    if degraded:
        response.status_code = 503
        return {"status": "degraded", "checks": checks}
    return {"status": "ok", "checks": checks}


# ── 管理员手动触发（Gap 1: 灰度验证用） ──────────────────────────────


@router.post("/admin/trigger/review_quick")
async def trigger_review_quick(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发 quick review（15:30 腾迅实时行情版）。
    供管理员灰度验证用。绕过 is_trading_day() 检查。
    """
    from aistock_agent.agents.workers.review import run_review

    report_date = _resolve_manual_report_date(body)
    trace_id = f"manual-quick-{report_date}-{int(time.time())}"
    logger = structlog.get_logger()
    logger.info("manual_trigger_review_quick", report_date=report_date, trace_id=trace_id)

    start = time.time()
    try:
        result = await run_review(
            report_date=report_date,
            snapshot_kind="quick",
            trace_id=trace_id,
        )
        elapsed = round(time.time() - start, 2)
        logger.info(
            "manual_trigger_review_quick_done",
            status=result.status, elapsed=elapsed, trace_id=trace_id,
        )
        return {
            "status": result.status,
            "report_date": result.report_date,
            "snapshot_kind": result.snapshot_kind,
            "trace_id": result.trace_id,
            "elapsed_seconds": elapsed,
            "markdown_preview": result.markdown[:200] if result.markdown else "",
        }
    except Exception as e:
        logger.error(
            "manual_trigger_review_quick_failed",
            error=str(e), exc_info=True, trace_id=trace_id,
        )
        raise HTTPException(status_code=502, detail=f"review_quick trigger failed: {e}")


@router.post("/admin/trigger/review_full")
async def trigger_review_full(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动触发 full review（20:30 Tushare 完整数据版）。
    供管理员灰度验证用。绕过 is_trading_day() 检查。
    """
    from aistock_agent.agents.workers.review import run_review

    report_date = _resolve_manual_report_date(body)
    trace_id = f"manual-full-{report_date}-{int(time.time())}"
    logger = structlog.get_logger()
    logger.info("manual_trigger_review_full", report_date=report_date, trace_id=trace_id)

    start = time.time()
    try:
        result = await run_review(
            report_date=report_date,
            snapshot_kind="full",
            trace_id=trace_id,
        )
        elapsed = round(time.time() - start, 2)
        logger.info(
            "manual_trigger_review_full_done",
            status=result.status, elapsed=elapsed, trace_id=trace_id,
        )
        return {
            "status": result.status,
            "report_date": result.report_date,
            "snapshot_kind": result.snapshot_kind,
            "trace_id": result.trace_id,
            "elapsed_seconds": elapsed,
            "markdown_preview": result.markdown[:200] if result.markdown else "",
        }
    except Exception as e:
        logger.error(
            "manual_trigger_review_full_failed",
            error=str(e), exc_info=True, trace_id=trace_id,
        )
        raise HTTPException(status_code=502, detail=f"review_full trigger failed: {e}")


@router.post("/admin/trigger/evening_chain")
async def trigger_evening_chain(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """一键补跑完整晚间链路（review → market_snapshot → iterate → evening Brief → broadcast）。

    供管理员在错过 15:30 调度或灰度验证时使用。review 阶段命中 Redis 缓存
    （TTL 2h）时快速返回；显式传入 report_date 时跳过交易日检查。
    返回各阶段状态，供前端/日志诊断。
    """
    from aistock_agent.services.scheduler import _run_evening_chain_task

    report_date = _resolve_manual_report_date(body)
    trace_id = f"manual-evening-{report_date}-{int(time.time())}"
    logger = structlog.get_logger()
    logger.info("manual_trigger_evening_chain_start", report_date=report_date, trace_id=trace_id)

    start = time.time()
    try:
        result = await _run_evening_chain_task(report_date=report_date)
        elapsed = round(time.time() - start, 2)
        logger.info(
            "manual_trigger_evening_chain_done",
            status=result.get("status"),
            report_date=report_date,
            elapsed=elapsed,
            trace_id=trace_id,
        )
        return {
            "status": result.get("status"),
            "report_date": result.get("report_date", report_date),
            "stages": result.get("stages"),
            "trace_id": trace_id,
            "elapsed_seconds": elapsed,
        }
    except Exception as e:
        logger.error(
            "manual_trigger_evening_chain_failed",
            error=str(e), exc_info=True, trace_id=trace_id,
        )
        raise HTTPException(status_code=502, detail=f"evening_chain trigger failed: {e}")


@router.post("/admin/trigger/midday")
async def trigger_midday(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """手动补跑盘中报任务（12:05 调度）。

    供管理员在错过 12:05 调度或验收时使用。仍会经过交易日守卫
    （_run_midday_task 内部校验，非交易日返回 skipped）。
    返回任务状态（skipped/ok/partial/failed），供前端/日志诊断。
    """
    from aistock_agent.services.scheduler import _run_midday_task

    report_date = _resolve_manual_report_date(body)
    trace_id = f"manual-midday-{report_date}-{int(time.time())}"
    logger = structlog.get_logger()
    logger.info("manual_trigger_midday_start", report_date=report_date, trace_id=trace_id)

    start = time.time()
    try:
        result = await _run_midday_task(report_date=report_date)
        elapsed = round(time.time() - start, 2)
        logger.info(
            "manual_trigger_midday_done",
            status=result.get("status"),
            report_date=report_date,
            elapsed=elapsed,
            trace_id=trace_id,
        )
        return {
            "status": result.get("status"),
            "report_date": result.get("report_date", report_date),
            "reason": result.get("reason"),
            "trace_id": trace_id,
            "elapsed_seconds": elapsed,
        }
    except Exception as e:
        logger.error(
            "manual_trigger_midday_failed",
            error=str(e), exc_info=True, trace_id=trace_id,
        )
        raise HTTPException(status_code=502, detail=f"midday trigger failed: {e}")


@router.post("/admin/stock-trace/dlq/replay")
async def replay_stock_trace_dlq(
    _: None = Depends(verify_internal_token),
    error_code: str = "",
    job_id: str = "",
    limit: int = 50,
) -> dict[str, object]:
    """把 DLQ 中可重投的死信重新入队回 stock-trace.jobs 主流。

    结构性错误码（INVALID_JOB_MESSAGE 等）不在 REPLAYABLE_ERROR_CODES 白名单，
    将直接跳过。error_code / job_id 可选，用于精确筛选。
    """
    import redis.asyncio as _aioredis

    from aistock_agent.workers.stock_trace_consumer import replay_dlq

    client = _aioredis.from_url(  # type: ignore[no-untyped-call]
        settings.stock_trace_redis_url, max_connections=2
    )
    filter_criteria: dict[str, str] = {}
    if error_code:
        filter_criteria["error_code"] = error_code
    if job_id:
        filter_criteria["job_id"] = job_id
    try:
        limit = max(1, min(limit, 200))
        replayed = await replay_dlq(client, filter_criteria, limit)
    finally:
        await client.aclose()
    return {"replayed": replayed}


# ── 大盘溯源后接预测（独立触发，PR-A/T5；T6 regenerate 代理的转发目标） ──


@router.post("/internal/predictions/from-trace")
async def trigger_predictions_from_trace(
    body: dict[str, str] | None = None,
    _: None = Depends(verify_internal_token),
) -> dict[str, object]:
    """大盘溯源后接预测独立触发端点（供 app-api /regenerate 代理转发）。

    流程：trade_date 校验 → 已验证拒覆盖（409，SPEC S6）→ predict_from_trace →
    TraceUnavailableError 落 skipped 记录并返回 200（硬约束 7：不静默缺失）。
    成功/llm_failed/parse_failed 均返回 ``{status, reason, record}``（后两者
    record=None 不落库，调用方（T6 代理）可重试）；意外异常兜底 502（"永不 500"）。
    """
    from aistock_agent.services.prediction_service import (
        TraceUnavailableError,
        predict_from_trace,
        save_skipped_prediction,
    )

    if not body or not body.get("trade_date"):
        raise HTTPException(status_code=400, detail="trade_date must be YYYY-MM-DD")
    trade_date = body["trade_date"]
    try:
        parsed_date = date.fromisoformat(trade_date)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="trade_date must be YYYY-MM-DD") from exc
    if parsed_date.isoformat() != trade_date:
        raise HTTPException(status_code=400, detail="trade_date must be YYYY-MM-DD")

    trace_id = body.get("trace_id") or "manual-regenerate"
    logger = structlog.get_logger()
    logger.info("predictions_from_trace_start", trade_date=trade_date, trace_id=trace_id)
    try:
        # 已验证拒覆盖防御（SPEC S6）：同交易日已有记录且 verification 非空 dict
        # （对齐 app-api Object.keys 语义）→ 拒绝覆盖，避免验证过的预测被静默重写
        existing = await node_api.list_predictions(f"review:{trade_date}")
        for record in existing:
            verification = record.get("verification")
            if isinstance(verification, dict) and verification:
                raise HTTPException(status_code=409, detail="已验证预测拒绝覆盖")

        result, record = await predict_from_trace(trace_id, trade_date)
        logger.info(
            "predictions_from_trace_done",
            trade_date=trade_date,
            trace_id=trace_id,
            status=result.status,
        )
        return {"status": result.status, "reason": result.reason, "record": record}
    except TraceUnavailableError as exc:
        # 溯源数据不可用：落 skipped 记录并原样暴露原因，不静默缺失（硬约束 7）
        skipped = await save_skipped_prediction(f"review:{trade_date}", str(exc))
        logger.warning(
            "predictions_from_trace_trace_unavailable",
            trade_date=trade_date,
            trace_id=trace_id,
            reason=str(exc),
        )
        return {"status": "skipped", "reason": str(exc), "record": skipped}
    except HTTPException:
        # 409/400 等业务拒绝直接透传
        raise
    except Exception as exc:
        logger.error(
            "predictions_from_trace_failed",
            trade_date=trade_date,
            trace_id=trace_id,
            error=str(exc),
            exc_info=True,
        )
        raise HTTPException(status_code=502, detail=f"predictions from-trace failed: {exc}")


@router.post("/qa")
async def qa_endpoint(req: QARequest) -> StreamingResponse:
    """CHAT QA 链路 SSE 端点。

    事件类型：evidence / token / insight / error / done
    """
    thread_id = req.thread_id or str(uuid4())

    initial_state: QuestionState = {
        "messages": [HumanMessage(content=req.message)],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "clarification": None,
    }

    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 25}

    async def event_stream():
        import time as _time

        e2e_start = _time.monotonic()
        try:
            graph = compile_chat_graph()
            async for event in graph.astream_events(
                initial_state, config=config, version="v2"
            ):
                event_name = event.get("event", "")
                node_name = event.get("name", "")

                # skill_executor 完成时推送 evidence
                if event_name == "on_chain_end" and node_name == "skill_executor":
                    output = event.get("data", {}).get("output", {})
                    evidences = (
                        output.get("evidences", [])
                        if isinstance(output, dict)
                        else []
                    )
                    for ev in evidences:
                        yield (
                            f"event: evidence\ndata: "
                            f"{json.dumps(ev.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                        )

                # synth_answer 流式 token
                elif event_name == "on_chat_model_stream":
                    chunk = event.get("data", {}).get("chunk")
                    if chunk and hasattr(chunk, "content") and chunk.content:
                        yield (
                            f"event: token\ndata: "
                            f"{json.dumps({'delta': chunk.content}, ensure_ascii=False)}\n\n"
                        )

                # synth_answer 完成时推送 insight
                elif event_name == "on_chain_end" and node_name == "synth_answer":
                    output = event.get("data", {}).get("output", {})
                    if isinstance(output, dict) and output.get("insight"):
                        insight = output["insight"]
                        yield (
                            f"event: insight\ndata: "
                            f"{json.dumps(insight.model_dump(mode='json'), ensure_ascii=False)}\n\n"
                        )

            yield "event: done\ndata: {}\n\n"
        except Exception as exc:
            _qa_logger.error("qa_endpoint.failed", err=str(exc), exc_info=True)
            yield f"event: error\ndata: {json.dumps({'message': str(exc)}, ensure_ascii=False)}\n\n"
        finally:
            _metrics.record_chat_qa_latency("e2e", int((_time.monotonic() - e2e_start) * 1000))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
