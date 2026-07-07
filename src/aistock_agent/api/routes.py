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

    result = await graph.ainvoke(initial_state)

    content = result.get("final_response") or "抱歉，我暂时无法处理您的请求。"
    return ChatResponse(content=content, session_id=session_id)


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
