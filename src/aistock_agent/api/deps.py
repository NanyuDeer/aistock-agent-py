"""FastAPI 依赖注入 —— 内网鉴权、初始状态构造、Redis 客户端

把原本散落在 ``api/routes.py`` 的鉴权函数与 state 构造抽离为可复用依赖，
为 Task 10 的 ``/chat/stream`` SSE 端点和 Phase 5 的 lifespan Redis 池做准备。

- ``verify_internal_token``：从 ``routes._verify_internal_token`` 迁入，行为不变
  （403 + detail="Forbidden"），改为用 FastAPI ``Depends`` 注入。
- ``build_initial_state``：从 ``/chat/message`` handler 抽出的 initial state 构造，
  字段名与默认值与原内联实现完全一致。
- ``get_redis_client``：暂用 ``aioredis.from_url``，Phase 5 改 lifespan 池。
"""
from __future__ import annotations

import redis.asyncio as aioredis
from fastapi import Header, HTTPException

from aistock_agent.config import settings


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
        "final_response": None,
    }


def get_redis_client() -> aioredis.Redis:
    """获取 Redis 客户端（暂用 from_url，Phase 5 改 lifespan 池）。

    与 ``agents/workers/morning.py`` 的 ``aioredis.from_url(settings.redis_url)``
    方式保持一致。
    """
    return aioredis.from_url(settings.redis_url)  # type: ignore[no-untyped-call, no-any-return]
