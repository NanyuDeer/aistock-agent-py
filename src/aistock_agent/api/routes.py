"""REST 接口 — 对话消息、晨报、工具列表"""

import json
from collections.abc import AsyncGenerator

from fastapi import APIRouter, Depends
from sse_starlette.sse import EventSourceResponse

from aistock_agent.agents.workers import morning as morning_agent
from aistock_agent.api.deps import build_initial_state, verify_internal_token
from aistock_agent.constants import SSEEventType
from aistock_agent.graph.builder import compile_graph
from aistock_agent.schemas.chat import ChatRequest, ChatResponse
from aistock_agent.utils.sse import map_langgraph_event_to_sse

router = APIRouter()


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


@router.get("/skills")
async def list_skills() -> dict[str, list[dict[str, str]]]:
    """已注册工具列表"""
    from aistock_agent.tools.market_tools import get_global_markets, tavily_finance_search
    from aistock_agent.tools.news_tools import get_cls_news, get_news_fulltext, search_cls_news
    from aistock_agent.tools.sector_tools import get_leader_stocks
    from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote

    all_tools = [
        get_quote, get_capital_flow, get_profit_forecast,
        get_leader_stocks,
        search_cls_news, get_news_fulltext, get_cls_news,
        get_global_markets, tavily_finance_search,
    ]

    return {
        "tools": [
            {"name": t.name, "description": t.description}
            for t in all_tools
        ]
    }
