"""事件传导分析执行服务

提供可复用的事件执行函数，供 scheduler 和手动 Morning 入口共同调用。
不依赖 scheduler 的私有函数，也不让 API route 复制整段 state 构造。

核心函数：
- ``run_single_event_conduction``：执行单个事件的传导分析
- ``run_event_conduction_batch``：并行执行多个事件，单个失败不阻断其他
"""

import asyncio
import hashlib
from dataclasses import dataclass, field
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
class AnalysisReportPayload:
    """事件传导分析内容载体 — 供 GI 使用。

    仅包含 event_conduction Agent 已生成的分析内容字段，
    不包含 success/cached/persisted/error 等任务状态字段。
    """

    event_id: str
    summary: str
    original_event: str
    impact_industries: list[str]
    impact_chain: list[dict[str, object]]
    key_variables: list[dict[str, object]]
    mechanism: str
    investment_rating: str
    investment_conclusion: str


@dataclass
class EventConductionResult:
    """单个事件传导分析的状态结果。"""

    success: bool
    event_id: str
    title: str
    event_generated: bool
    persisted: bool = False
    cached: bool = False
    error: str | None = None


@dataclass
class EventConductionOutput:
    """事件传导分析的完整输出：状态 + 分析内容。

    status: 任务状态（success/persisted/cached/error 等）。
    analysis_report: 分析内容（仅成功时有值），供 GI 等下游消费。
    """

    status: EventConductionResult
    analysis_report: AnalysisReportPayload | None = None


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
) -> EventConductionOutput:
    """执行单个事件的传导分析。

    构建事件消息 → 调用 event_agent.run() → 返回结构化结果。
    供 scheduler 和手动 Morning 入口共同调用。

    只读取 event_agent 返回的显式状态字段（event_generated/event_persisted/event_id），
    **禁止**通过 final_response 非空或虚构字段推断成功。

    Args:
        event: major_event dict，含 title/summary/url

    Returns:
        EventConductionOutput（status + analysis_report）
    """
    title = str(event.get("title", "")).strip()
    if not title:
        return EventConductionOutput(
            status=EventConductionResult(
                success=False,
                event_id="",
                title="",
                event_generated=False,
                persisted=False,
                error="event has empty title, skipped",
            ),
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

        status = EventConductionResult(
            success=success,
            event_id=agent_event_id,
            title=title,
            event_generated=event_generated,
            persisted=persisted,
            cached=cached,
            error=None if success else "event agent did not generate a valid report",
        )

        # ── 提取分析内容供 GI 使用 ──
        analysis_report = _extract_analysis_payload(analysis_reports, agent_event_id, user_message)

        return EventConductionOutput(status=status, analysis_report=analysis_report)
    except Exception as e:
        logger.error(
            "event_conduction_failed",
            title=title[:50],
            error=str(e),
            exc_info=True,
        )
        return EventConductionOutput(
            status=EventConductionResult(
                success=False,
                event_id=event_id,
                title=title,
                event_generated=False,
                persisted=False,
                error=str(e),
            ),
        )


async def run_event_conduction_batch(
    major_events: list[dict[str, object]],
) -> list[EventConductionOutput]:
    """并行执行多个事件的传导分析。

    使用 asyncio.gather(return_exceptions=True) — 单事件失败/异常被隔离：
    1. 正常路径：run_single_event_conduction 内部 try-catch 捕获业务异常，
       返回 EventConductionOutput(status=EventConductionResult(success=False))。
    2. 防御路径：gather 的 return_exceptions=True 兜底任何未被内部捕获的异常
       （如取消、意外的非业务异常），保证不会因单个事件中断整个批次。
    3. 最终结果统一映射为 EventConductionOutput（异常→success=False），
       顺序与输入事件一一对应。

    Args:
        major_events: major_event dict 列表

    Returns:
        与输入等长的 EventConductionOutput 列表，顺序对应输入事件
    """
    if not major_events:
        return []

    tasks = [run_single_event_conduction(event) for event in major_events]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    return [_as_result(r, event) for r, event in zip(raw_results, major_events)]


def _as_result(raw: object, event: dict[str, object]) -> EventConductionOutput:
    """将 gather 原始结果（EventConductionOutput 或异常）统一映射为 EventConductionOutput。"""
    if isinstance(raw, EventConductionOutput):
        return raw
    if isinstance(raw, BaseException):
        logger.warning(
            "event_conduction_item_unexpected_exception",
            title=str(event.get("title", ""))[:50],
            error=str(raw),
        )
        return EventConductionOutput(
            status=EventConductionResult(
                success=False,
                event_id="",
                title=str(event.get("title", "")),
                event_generated=False,
                persisted=False,
                error=f"unexpected exception: {raw}",
            ),
        )
    # 理论不可达：run_single_event_conduction 恒返回 EventConductionOutput
    return EventConductionOutput(
        status=EventConductionResult(
            success=False,
            event_id="",
            title=str(event.get("title", "")),
            event_generated=False,
            persisted=False,
            error=f"unexpected result type: {type(raw).__name__}",
        ),
    )


# ── 分析内容提取 ──


def _extract_analysis_payload(
    analysis_reports: dict[str, object],
    event_id: str,
    user_message: str,
) -> AnalysisReportPayload:
    """从 agent 返回的 analysis_reports 中提取 GI 所需的完整分析字段。

    对应 _extract_event_input() 的 DB 路径提取逻辑，
    但数据来源为 event_agent.run() 返回的内存 analysis_reports，
    而非 PostgreSQL 落库后的 content JSONB。
    """
    understanding = analysis_reports.get("event_understanding")
    transmission = analysis_reports.get("event_transmission")
    investment = analysis_reports.get("event_investment")

    # summary: event_understanding.summary
    summary = ""
    if isinstance(understanding, dict):
        summary = str(understanding.get("summary", ""))

    # mechanism: event_transmission.mechanism
    mechanism = ""
    if isinstance(transmission, dict):
        mechanism = str(transmission.get("mechanism", ""))

    # impact_chain: event_transmission.chain[] → [{industry, direction, impact_strength}]
    impact_chain: list[dict[str, object]] = []
    impact_industries: list[str] = []
    if isinstance(transmission, dict):
        raw_chain = transmission.get("chain", [])
        if isinstance(raw_chain, list):
            for item in raw_chain:
                if not isinstance(item, dict):
                    continue
                industry = str(item.get("industry", ""))
                if not industry:
                    continue
                impact_chain.append({
                    "industry": industry,
                    "direction": str(item.get("direction", "")),
                    "impact_strength": (
                        float(item.get("impactStrength", 0))
                        if item.get("impactStrength") is not None
                        else 0.0
                    ),
                })
            # 从 chain[] 提取行业名称去重
            impact_industries = list({
                c["industry"] for c in impact_chain if isinstance(c, dict)
            })

    # key_variables: event_transmission.variables[] → [{name, direction, strength}]
    key_variables: list[dict[str, object]] = []
    if isinstance(transmission, dict):
        raw_vars = transmission.get("variables", [])
        if isinstance(raw_vars, list):
            for v in raw_vars:
                if not isinstance(v, dict):
                    continue
                key_variables.append({
                    "name": str(v.get("name", "")),
                    "direction": str(v.get("direction", "")),
                    "strength": (
                        float(v.get("strength", 0))
                        if v.get("strength") is not None
                        else 0.0
                    ),
                })

    # investment_rating: event_investment.rating
    investment_rating = ""
    if isinstance(investment, dict):
        investment_rating = str(investment.get("rating", ""))

    # investment_conclusion: event_investment.conclusion
    investment_conclusion = ""
    if isinstance(investment, dict):
        investment_conclusion = str(investment.get("conclusion", ""))

    return AnalysisReportPayload(
        event_id=event_id,
        summary=summary,
        original_event=user_message,
        impact_industries=impact_industries,
        impact_chain=impact_chain,
        key_variables=key_variables,
        mechanism=mechanism,
        investment_rating=investment_rating,
        investment_conclusion=investment_conclusion,
    )
