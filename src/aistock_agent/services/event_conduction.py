"""事件传导分析执行服务

提供可复用的事件执行函数，供 scheduler 和手动 Morning 入口共同调用。
不依赖 scheduler 的私有函数，也不让 API route 复制整段 state 构造。

核心函数：
- ``run_single_event_conduction``：执行单个事件的传导分析
- ``run_event_conduction_batch``：并行执行多个事件，单个失败不阻断其他
"""

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import date

import structlog

from aistock_agent.state.schema import AgentState

logger = structlog.get_logger()

# 已知的降级/空消息文案——出现这些则视为失败
_DEGRADED_RESPONSES: frozenset[str] = frozenset({
    "事件分析暂时不可用，请稍后重试",
    "请提供需要分析的事件描述。",
})


@dataclass
class EventConductionResult:
    """单个事件传导分析的结果。"""

    success: bool
    event_id: str
    title: str
    event_generated: bool
    persisted: bool = False
    cached: bool = False
    error: str | None = None


def _build_event_message(event: dict[str, object]) -> str:
    """从 major_event dict 构建用户消息文本。"""
    title = str(event.get("title", "未知事件"))
    summary = str(event.get("summary", ""))
    url = str(event.get("url", ""))

    user_message = f"请分析以下重大事件：{title}"
    if summary:
        user_message += f"\n\n事件概述：{summary}"
    if url:
        user_message += f"\n\n原文链接：{url}"
    return user_message


def _is_degraded(final_response: str, analysis_reports: dict[str, object]) -> bool:
    """判断 event_agent.run() 返回是否为降级/失败。

    优先使用显式状态 ``event_generated``，仅在缺失时回退到文案检查。
    """
    # 显式状态优先
    if "event_generated" in analysis_reports:
        return not bool(analysis_reports["event_generated"])
    # 回退检查（兼容旧版缓存）
    if final_response in _DEGRADED_RESPONSES:
        return True
    if not analysis_reports:
        return True
    return False


async def run_single_event_conduction(
    event: dict[str, object],
) -> EventConductionResult:
    """执行单个事件的传导分析。

    构建事件消息 → 调用 event_agent.run() → 返回结构化结果。
    供 scheduler 和手动 Morning 入口共同调用。

    只读取 event_agent 返回的显式状态字段（event_generated/event_persisted/event_id），
    **禁止**通过 final_response 非空或虚构字段推断成功。

    Args:
        event: major_event dict，含 title/summary/url

    Returns:
        EventConductionResult with success/event_generated/persisted/error
    """
    title = str(event.get("title", "")).strip()
    if not title:
        return EventConductionResult(
            success=False,
            event_id="",
            title="",
            event_generated=False,
            persisted=False,
            error="event has empty title, skipped",
        )

    logger.info("event_conduction_start", title=title[:50])

    user_message = _build_event_message(event)
    event_id = f"evt_{hashlib.md5(user_message.encode()).hexdigest()[:8]}"

    # 来源元数据：从 major_events 的 url 字段提取，通过 state.analysis_reports.event_source
    # 传递给 event agent，使后者能在 event_meta.source 中落库真实来源 URL。
    event_source = str(event.get("url", ""))

    state: AgentState = {
        "messages": [{"role": "user", "content": user_message}],
        "session_id": f"event_{date.today().isoformat()}_{title[:20]}",
        "user_id": None,
        "favorites": [],
        "intent": "event",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {"event_source": event_source},
        "final_response": None,
    }

    try:
        # 延迟 import 避免循环依赖
        from aistock_agent.agents.workers import event as event_agent

        result = await event_agent.run(state)
        analysis_reports = result.get("analysis_reports", {})
        if not isinstance(analysis_reports, dict):
            analysis_reports = {}

        # 只读显式状态字段，不推断
        event_generated = bool(analysis_reports.get("event_generated", False))
        persisted = bool(analysis_reports.get("event_persisted", False))
        cached = bool(analysis_reports.get("event_cached", False))
        # event_id 优先从 agent 返回获取（缓存命中时可能不同）
        agent_event_id = str(analysis_reports.get("event_id", event_id))
        success = event_generated

        logger.info(
            "event_conduction_done",
            title=title[:50],
            event_generated=event_generated,
            persisted=persisted,
            cached=cached,
            success=success,
        )

        return EventConductionResult(
            success=success,
            event_id=agent_event_id,
            title=title,
            event_generated=event_generated,
            persisted=persisted,
            cached=cached,
            error=None if success else "event agent did not generate a valid report",
        )
    except Exception as e:
        logger.error(
            "event_conduction_failed",
            title=title[:50],
            error=str(e),
            exc_info=True,
        )
        return EventConductionResult(
            success=False,
            event_id=event_id,
            title=title,
            event_generated=False,
            persisted=False,
            error=str(e),
        )


async def run_event_conduction_batch(
    major_events: list[dict[str, object]],
) -> list[EventConductionResult]:
    """并行执行多个事件的传导分析。

    使用 asyncio.gather(return_exceptions=False) — 每个事件内部的异常
    已被 run_single_event_conduction 捕获并转化为 EventConductionResult，
    因此不会因单个事件异常中断整个批次。

    Args:
        major_events: major_event dict 列表

    Returns:
        与输入等长的 EventConductionResult 列表，顺序对应输入事件
    """
    if not major_events:
        return []

    tasks = [run_single_event_conduction(event) for event in major_events]
    results = await asyncio.gather(*tasks)
    return list(results)
