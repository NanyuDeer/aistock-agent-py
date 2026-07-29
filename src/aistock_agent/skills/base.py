"""Skill 协议与 @skill 装饰器。

@skill 统一处理异常→degraded Evidence、日志、耗时记录，
Skill 实现只关心业务逻辑，不重复 try/except。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

import structlog

from aistock_agent.schemas.chat_contract import Evidence, InsightGoal

logger = structlog.get_logger()


@runtime_checkable
class SkillProtocol(Protocol):
    """Skill 协议：可调用对象，接收 args + goal，返回 Evidence。"""

    name: str

    async def __call__(
        self, args: dict[str, Any], goal: InsightGoal
    ) -> Evidence: ...


def skill(
    func: Callable[..., Awaitable[Evidence]],
) -> Callable[..., Awaitable[Evidence]]:
    """装饰 Skill 函数，统一异常→degraded、日志、耗时记录。"""

    @wraps(func)
    async def wrapper(*args: Any, **kwargs: Any) -> Evidence:
        start = time.monotonic()
        try:
            ev = await func(*args, **kwargs)
            ms = int((time.monotonic() - start) * 1000)
            logger.info(
                "skill.ok",
                skill=func.__name__,
                ms=ms,
                degraded=ev.degraded,
            )
            return ev
        except Exception as exc:
            ms = int((time.monotonic() - start) * 1000)
            logger.warning(
                "skill.fail",
                skill=func.__name__,
                ms=ms,
                err=str(exc),
                exc_info=True,
            )
            return Evidence(
                facts=[],
                sources=[],
                as_of=datetime.now(timezone.utc),
                degraded=True,
                degraded_reason=f"{func.__name__}: {exc}",
                skill_name=func.__name__,
                raw={},
            )

    return wrapper
