"""Skill 协议与 @skill 装饰器。

@skill 统一处理异常→degraded Evidence、日志、耗时记录，
Skill 实现只关心业务逻辑，不重复 try/except。
"""
from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Protocol, runtime_checkable

import structlog

from aistock_agent.observability.metrics import get_metrics_collector
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
        metrics = get_metrics_collector()
        try:
            ev = await func(*args, **kwargs)
            ms = int((time.monotonic() - start) * 1000)
            metrics.record_skill_latency(func.__name__, ms)
            if ev.degraded:
                metrics.record_skill_degraded(func.__name__)
            logger.info(
                "skill.ok",
                skill=func.__name__,
                ms=ms,
                degraded=ev.degraded,
            )
            return ev
        except Exception as exc:
            ms = int((time.monotonic() - start) * 1000)
            metrics.record_skill_latency(func.__name__, ms)
            metrics.record_skill_degraded(func.__name__)
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
                as_of=datetime.now(UTC),
                degraded=True,
                degraded_reason=f"{func.__name__}: {exc}",
                skill_name=func.__name__,
                raw={},
            )

    return wrapper
