"""FastAPI 依赖注入 —— 内网鉴权、初始状态构造、Redis 客户端

把原本散落在 ``api/routes.py`` 的鉴权函数与 state 构造抽离为可复用依赖，
为 Task 10 的 ``/chat/stream`` SSE 端点和 Phase 5 的 lifespan Redis 池做准备。

- ``verify_internal_token``：从 ``routes._verify_internal_token`` 迁入，行为不变
  （403 + detail="Forbidden"），改为用 FastAPI ``Depends`` 注入。
- ``build_initial_state``：从 ``/chat/message`` handler 抽出的 initial state 构造，
  字段名与默认值与原内联实现完全一致。
- ``get_redis_client``：从 lifespan 管理的 ``RedisPool`` 获取客户端单例。
"""
from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import Header, HTTPException
from langchain_core.messages import HumanMessage

from aistock_agent.config import settings
from aistock_agent.services.redis_pool import RedisPool
from aistock_agent.state.chat_schema import QuestionState


def verify_internal_token(
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
) -> None:
    """验证内网鉴权 token。

    缺失或不匹配时抛 403（与原 ``_verify_internal_token`` 行为一致）。
    成功返回 None —— 仅做校验，不向端点注入值。
    """
    if x_internal_token != settings.internal_api_token:
        raise HTTPException(status_code=403, detail="Forbidden")


def build_initial_state(
    message: str,
    session_id: str | None,
    user_id: str | None,
    favorites: list[str],
) -> dict[str, object]:
    """构造 /chat/message 的 LangGraph 初始状态。

    原样抽出 ``routes.chat_message`` 内联的 state 构造：字段名、messages
    格式、默认值（intent/symbol/tag_code=None、analysis_reports={}、
    final_response=None）均与重构前一致。

    Phase 5 新增预加载字段：wind_leaders_data、institution_research_data，
    初始为 None，Agent 按需通过 node_api 加载。
    """
    return {
        "messages": [{"role": "user", "content": message}],
        "session_id": session_id,
        "user_id": user_id,
        "favorites": favorites,
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "wind_leaders_data": None,  # 预加载字段（Agent按需加载）
        "institution_research_data": None,  # 预加载字段（Agent按需加载）
        "final_response": None,
        "trigger_source": "user",  # 标记用户请求来源，使 intent_router 能路由到 ai_advisor
    }


def build_chat_initial_state(message: str) -> QuestionState:
    """构造 /chat/* 路由切换到新 CHAT 子图时的初始状态。

    与 /qa 端点的 initial_state 结构对齐（routes.py 的 qa_endpoint）。
    /chat/* 路由的 session_id 通过 thread_id（config 参数）传递给 checkpointer，
    不放入 QuestionState（新子图不使用该字段）。
    """
    return {
        "messages": [HumanMessage(content=message)],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }


async def get_redis_client() -> aioredis.Redis:
    """获取 Redis 客户端（从 lifespan 管理的连接池单例）。

    与 ``services.cache`` 共享同一个 ``RedisPool`` 客户端实例，
    由 ``main.lifespan`` 在启动时初始化。
    """
    return await RedisPool.get_client()
