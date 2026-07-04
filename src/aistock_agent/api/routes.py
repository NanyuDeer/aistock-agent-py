"""REST 接口 — 对话消息、晨报、工具列表"""

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from aistock_agent.config import settings
from aistock_agent.graph.builder import compile_graph

router = APIRouter()


class ChatRequest(BaseModel):
    """对话请求"""
    message: str
    session_id: Optional[str] = None
    user_id: Optional[str] = None
    favorites: list[str] = []


class ChatResponse(BaseModel):
    """对话响应"""
    content: str
    session_id: str


def _verify_internal_token(x_internal_token: Optional[str] = Header(None)) -> None:
    """验证内网鉴权 token"""
    if x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Forbidden")


@router.post("/chat/message", response_model=ChatResponse)
async def chat_message(req: ChatRequest) -> ChatResponse:
    """对话消息（非流式）"""
    graph = compile_graph()

    session_id = req.session_id or f"session_{id(req)}"

    initial_state = {
        "messages": [{"role": "user", "content": req.message}],
        "session_id": session_id,
        "user_id": req.user_id,
        "favorites": req.favorites,
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    result = await graph.ainvoke(initial_state)

    content = result.get("final_response") or "抱歉，我暂时无法处理您的请求。"
    return ChatResponse(content=content, session_id=session_id)


@router.get("/briefing/morning")
async def morning_briefing() -> dict:
    """晨报（非流式，支持 Redis 缓存）"""
    graph = compile_graph()

    initial_state = {
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

    result = await graph.ainvoke(initial_state)

    content = result.get("final_response") or "晨报生成失败，请稍后重试。"
    return {"content": content}


@router.get("/skills")
async def list_skills() -> dict:
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
