"""定时任务调度器（APScheduler）

在进程启动时自动开启，通过 AsyncIOScheduler 管理所有定时任务：
- 08:50 晨报 analysis：morning agent（宏观策略4步框架，缓存+落盘）
  → 完成后自动提取 major_events，并行触发 event agent 传导分析（fire-and-forget）
- 09:00 播报链路 broadcast：串行执行 morning→wind_leader→hot_burst→trend_score→broadcast
  （报告写DB+双人语音，9:10前端可见）
- 15:30 晚间链路：review → market_snapshot → iterate → Brief → broadcast
"""

import asyncio
import json
from datetime import date

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from aistock_agent.config import settings
from aistock_agent.services.briefing import build_and_persist_brief
from aistock_agent.services.data_client import node_api
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.brief_contract import (
    build_iterate_brief_summary,
    build_market_snapshot_brief_summary,
)
from aistock_agent.utils.date import is_trading_day, shanghai_today

logger = structlog.get_logger()

_PERSISTABLE_ITERATE_STATUSES = frozenset({"normal", "alert"})

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """获取调度器单例（懒初始化，未 start 前仅创建实例）"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    return _scheduler


def start_scheduler() -> None:
    """注册早报、早间播报与单一晚间串行链路。"""
    if not settings.scheduler_enabled:
        logger.info("scheduler_disabled_by_config")
        return

    scheduler = get_scheduler()
    scheduler.add_job(
        _run_morning_task,
        CronTrigger.from_crontab(
            settings.scheduler_morning_cron,
            timezone=settings.scheduler_timezone,
        ),
        id="morning_briefing",
        name="morning briefing",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_broadcast_task,
        CronTrigger.from_crontab(
            settings.scheduler_broadcast_cron,
            timezone=settings.scheduler_timezone,
        ),
        id="broadcast_chain",
        name="morning broadcast chain",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_evening_chain_task,
        CronTrigger.from_crontab(
            settings.scheduler_review_cron,
            timezone=settings.scheduler_timezone,
        ),
        id="evening_chain",
        name="evening report chain",
        replace_existing=True,
    )
    scheduler.start()
    logger.info("scheduler_started", jobs=[job.id for job in scheduler.get_jobs()])


def shutdown_scheduler() -> None:
    """优雅停止调度器。

    若调度器已 start 则 shutdown(wait=True) 等待正在执行的任务完成；
    若仅 get_scheduler() 创建但未 start（state=STOPPED），直接置 None 避免触发
    SchedulerNotRunningError。幂等：多次调用安全。
    """
    global _scheduler
    if _scheduler is not None:
        # running 属性 = state != STOPPED；未 start 的调度器 running=False
        if _scheduler.running:
            _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("scheduler_stopped")


# ─── 定时任务执行函数 ───


async def _run_morning_task() -> None:
    """晨报生成任务（交易日 08:50）。

    非交易日直接跳过；交易日构造 AgentState 调用 morning.run()，
    结果写入 Redis 缓存（由 morning agent 内部处理）。
    """
    report_day = shanghai_today()
    report_date = report_day.isoformat()
    if not is_trading_day(report_day):
        logger.info("scheduler_skip_non_trading_day", task="morning")
        return

    logger.info("scheduler_morning_start")
    # 函数内 import：延迟加载 LangGraph/LangChain 重依赖，避免 scheduler 模块加载时拖慢启动
    from aistock_agent.agents.workers import morning as morning_agent

    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_morning_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": "morning",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "report_date": report_date,
        "final_response": None,
    }

    try:
        result = await morning_agent.run(state)
        logger.info(
            "scheduler_morning_done",
            has_response=bool(result.get("final_response")),
        )

        # 提取重大事件列表，并行触发事件传导分析（fire-and-forget）
        # morning agent 在 analysis_reports 中写入 major_events 列表（Task 6 产出）
        analysis_reports = result.get("analysis_reports")
        raw_major_events = (
            analysis_reports.get("major_events", []) if isinstance(analysis_reports, dict) else []
        )
        major_events: list[dict[str, object]] = (
            [event for event in raw_major_events if isinstance(event, dict)]
            if isinstance(raw_major_events, list)
            else []
        )
        if major_events:
            event_tasks = [
                asyncio.create_task(_run_event_task(event))
                for event in major_events
                if event.get("title")
            ]
            logger.info(
                "scheduler_event_triggered",
                total=len(event_tasks),
                titles=[
                    title[:30]
                    for event in major_events
                    if isinstance((title := event.get("title")), str)
                ],
            )
            # fire-and-forget: 各个事件 task 独立在后台运行，错误由 _run_event_task 内部捕获
    except Exception as e:
        logger.error("scheduler_morning_failed", error=str(e), exc_info=True)


def _make_scheduled_state(
    report_date: str,
    *,
    intent: str | None = None,
    brief_type: str | None = None,
) -> AgentState:
    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_{intent or brief_type or 'report'}_{report_date}",
        "user_id": None,
        "favorites": [],
        "intent": intent,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "trigger_source": "scheduler",
        "report_date": report_date,
        "final_response": None,
    }
    if brief_type is not None:
        state["brief_type"] = brief_type
    return state


def _is_traceable_completed_report(report: object, expected_type: str) -> bool:
    """校验已完成上游工件具备聚合所需的最小追溯信息。"""
    if not isinstance(report, dict):
        return False

    report_id = report.get("id")
    has_valid_id = (
        isinstance(report_id, int) and not isinstance(report_id, bool) and report_id > 0
    ) or (isinstance(report_id, str) and bool(report_id.strip()))
    data_source = report.get("data_source")
    created_at = report.get("created_at")
    content = report.get("content")
    return (
        has_valid_id
        and report.get("report_type") == expected_type
        and report.get("status") == "completed"
        and isinstance(data_source, str)
        and bool(data_source.strip())
        and isinstance(created_at, str)
        and bool(created_at.strip())
        and isinstance(content, dict)
        and bool(content)
    )


async def _save_and_verify_report(
    *,
    report_type: str,
    report_date: str,
    data_source: str,
    content: dict[str, object],
) -> bool:
    """保存工件；保存响应元数据不全时回读数据库确认可追溯。"""
    saved = await node_api.save_analysis_report(
        report_type=report_type,
        report_date=report_date,
        data_source=data_source,
        content=content,
    )
    if saved is None:
        return False
    if _is_traceable_completed_report(saved, report_type):
        return True

    persisted = await node_api.get_analysis_report(report_type, report_date)
    return _is_traceable_completed_report(persisted, report_type)


def _extract_snapshot_summary(snapshot: object) -> str:
    """兼容测试：从受控 summary 读取已由代码构造的展示文本。"""
    summary = build_market_snapshot_brief_summary(snapshot)
    value = summary.get("summary") if isinstance(summary, dict) else None
    return value if isinstance(value, str) else ""


def _extract_iterate_summary(iterate_payload: object) -> str:
    """兼容测试：只读取由受控构造函数生成的 iterate 摘要。"""
    summary = build_iterate_brief_summary(iterate_payload)
    value = summary.get("summary") if isinstance(summary, dict) else None
    return value if isinstance(value, str) else ""


async def _run_evening_chain_task() -> None:
    """串行生成 review、market_snapshot、iterate、Brief 与晚间播报。"""
    report_day = shanghai_today()
    if not is_trading_day(report_day):
        logger.info("scheduler_skip_non_trading_day", task="evening_chain")
        return

    report_date = report_day.isoformat()
    from aistock_agent.agents.workers import broadcast as broadcast_agent
    from aistock_agent.agents.workers import iterate as iterate_agent
    from aistock_agent.agents.workers import review as review_agent
    from aistock_agent.services.snapshot_builder import build_snapshot

    try:
        await review_agent.run(_make_scheduled_state(report_date, intent="review"))
        review_report = await node_api.get_analysis_report("review", report_date)
        if not _is_traceable_completed_report(review_report, "review"):
            logger.error("scheduler_evening_review_invalid", report_date=report_date)
            return
    except Exception as exc:
        logger.error("scheduler_evening_review_failed", error=str(exc), exc_info=True)
        return

    try:
        snapshot = await asyncio.to_thread(build_snapshot, report_date)
        if not isinstance(snapshot, dict) or snapshot.get("error"):
            logger.error(
                "scheduler_evening_snapshot_invalid",
                report_date=report_date,
                error=(snapshot.get("error") if isinstance(snapshot, dict) else "invalid_payload"),
            )
            return

        # Brief 仅消费代码构造且可重建验证的 brief_summary.v1，不能读取原始快照。
        snapshot_summary = build_market_snapshot_brief_summary(snapshot)
        snapshot_content: dict[str, object] = {
            "brief_summary": snapshot_summary,
            "snapshot": snapshot,
        }
        if not await _save_and_verify_report(
            report_type="market_snapshot",
            report_date=report_date,
            data_source="snapshot_builder",
            content=snapshot_content,
        ):
            logger.error(
                "scheduler_evening_snapshot_not_traceable",
                report_date=report_date,
            )
            return
    except Exception as exc:
        logger.error("scheduler_evening_snapshot_failed", error=str(exc), exc_info=True)
        return

    try:
        iterate_result = await iterate_agent.run(_make_scheduled_state(report_date))
        iterate_text = str(iterate_result.get("final_response") or "")
        iterate_payload = json.loads(iterate_text)
        if (
            not isinstance(iterate_payload, dict)
            or iterate_payload.get("status") not in _PERSISTABLE_ITERATE_STATUSES
        ):
            logger.error("scheduler_evening_iterate_invalid", report_date=report_date)
            return

        # 原始 LLM payload 仅用于本次调度诊断；Brief 事实由代码受控构造。
        iterate_summary = build_iterate_brief_summary(iterate_payload)
        iterate_content: dict[str, object] = {
            "brief_summary": iterate_summary,
            "iterate_payload": iterate_payload,
        }
        if not await _save_and_verify_report(
            report_type="iterate",
            report_date=report_date,
            data_source="iterate_analyzer",
            content=iterate_content,
        ):
            logger.error(
                "scheduler_evening_iterate_not_traceable",
                report_date=report_date,
            )
            return
    except Exception as exc:
        logger.error("scheduler_evening_iterate_failed", error=str(exc), exc_info=True)
        return

    try:
        brief_saved = await build_and_persist_brief("evening", report_date)
    except Exception as exc:
        logger.error("scheduler_evening_brief_failed", error=str(exc), exc_info=True)
        brief_saved = False

    if brief_saved:
        try:
            await broadcast_agent.run(_make_scheduled_state(report_date, brief_type="evening"))
        except Exception as exc:
            logger.error("scheduler_evening_broadcast_failed", error=str(exc), exc_info=True)


async def _run_review_task() -> None:
    """复盘生成任务（交易日 15:30）"""
    if not is_trading_day(shanghai_today()):
        logger.info("scheduler_skip_non_trading_day", task="review")
        return

    logger.info("scheduler_review_start")
    from aistock_agent.agents.workers import review as review_agent

    today = date.today().isoformat()
    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_review_{date.today().isoformat()}",
        "user_id": None,
        "favorites": [],
        "intent": "review",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "trigger_source": "scheduler",
        "report_date": today,
        "final_response": None,
    }

    try:
        result = await review_agent.run(state)
        logger.info(
            "scheduler_review_done",
            has_response=bool(result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_review_failed", error=str(e), exc_info=True)


async def _run_snapshot_task() -> None:
    """快照生成任务（交易日 15:35）"""
    if not is_trading_day(shanghai_today()):
        logger.info("scheduler_skip_non_trading_day", task="snapshot")
        return

    logger.info("scheduler_snapshot_start")
    from aistock_agent.services.snapshot_builder import build_snapshot

    try:
        # build_snapshot 是同步函数（含同步 llm.invoke()），
        # 用 asyncio.to_thread 扔到线程池避免阻塞 AsyncIOScheduler 的事件循环
        snapshot = await asyncio.to_thread(build_snapshot)
        logger.info(
            "scheduler_snapshot_done",
            date=snapshot.get("date"),
            has_error=bool(snapshot.get("error")),
        )
    except Exception as e:
        logger.error("scheduler_snapshot_failed", error=str(e), exc_info=True)


async def _run_iterate_task() -> None:
    """迭代分析任务（交易日 15:40）"""
    if not is_trading_day(shanghai_today()):
        logger.info("scheduler_skip_non_trading_day", task="iterate")
        return

    logger.info("scheduler_iterate_start")
    from aistock_agent.agents.workers import iterate as iterate_agent

    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_iterate_{date.today().isoformat()}",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    try:
        result = await iterate_agent.run(state)
        logger.info(
            "scheduler_iterate_done",
            has_response=bool(result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_iterate_failed", error=str(e), exc_info=True)


async def _run_broadcast_task() -> None:
    """播报链路任务（交易日 09:00）。

    串行执行：morning → wind_leader → hot_burst → trend_score → broadcast。
    每个 Agent 设置 trigger_source="scheduler" + report_date，使报告写入数据库。
    broadcast 从数据库读取报告生成双人语音播报。

    每个 Agent 异常独立捕获，不影响后续 Agent 执行。
    morning 在 08:50 已执行过，此处命中缓存快速返回（或重新生成）。
    """
    if not is_trading_day(shanghai_today()):
        logger.info("scheduler_skip_non_trading_day", task="broadcast")
        return

    today = shanghai_today().isoformat()
    logger.info("scheduler_broadcast_chain_start", report_date=today)

    # 函数内 import：延迟加载重依赖
    from aistock_agent.agents.workers import broadcast as broadcast_agent
    from aistock_agent.agents.workers import hot_burst as hot_burst_agent
    from aistock_agent.agents.workers import morning as morning_agent
    from aistock_agent.agents.workers import trend_score as trend_score_agent
    from aistock_agent.agents.workers import wind_leader as wind_leader_agent

    def _make_state(intent: str | None = None) -> AgentState:
        """构造 scheduler 触发的 AgentState"""
        return {
            "messages": [],
            "session_id": f"scheduled_broadcast_{today}",
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

    # Step 1: 晨报（08:50 已执行过，命中缓存快速返回）
    try:
        morning_result = await morning_agent.run(_make_state("morning"))
        logger.info(
            "scheduler_broadcast_morning_done",
            has_response=bool(morning_result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_broadcast_morning_failed", error=str(e), exc_info=True)

    # Step 2: 风口分析
    try:
        wind_result = await wind_leader_agent.run(_make_state())
        logger.info(
            "scheduler_broadcast_wind_leader_done",
            has_response=bool(wind_result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_broadcast_wind_leader_failed", error=str(e), exc_info=True)

    # Step 3: 机构调研热门股
    try:
        burst_result = await hot_burst_agent.run(_make_state())
        logger.info(
            "scheduler_broadcast_hot_burst_done",
            has_response=bool(burst_result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_broadcast_hot_burst_failed", error=str(e), exc_info=True)

    # Step 3.5: 趋势股评分分析（写DB供 broadcast 消费 + 前端查询）
    try:
        trend_result = await trend_score_agent.run(_make_state())
        logger.info(
            "scheduler_broadcast_trend_score_done",
            has_response=bool(trend_result.get("final_response")),
        )
    except Exception as e:
        logger.error("scheduler_broadcast_trend_score_failed", error=str(e), exc_info=True)

    # Step 4: 播报生成（从数据库读取报告）
    try:
        if not await build_and_persist_brief("morning", today):
            logger.error("scheduler_morning_brief_not_persisted", report_date=today)
            return
        broadcast_result = await broadcast_agent.run({**_make_state(), "brief_type": "morning"})
        logger.info(
            "scheduler_broadcast_done",
            has_response=bool(broadcast_result.get("final_response")),
            has_audio=bool(broadcast_result.get("audio_path")),
        )
    except Exception as e:
        logger.error("scheduler_broadcast_final_failed", error=str(e), exc_info=True)


async def _run_event_task(event: dict[str, object]) -> None:
    """单个事件传导分析任务。

    由 morning 任务完成后触发，每个 major_event 一个独立 task，
    所有事件并行执行（asyncio.create_task）。失败不影响其他事件。

    委托给 services.event_conduction.run_single_event_conduction，
    避免在 scheduler 中重复 state 构造逻辑。

    Args:
        event: major_event dict，含 title/summary/url/impact_score/direction/involved_keywords
    """
    from aistock_agent.services.event_conduction import run_single_event_conduction

    title = str(event.get("title", "未知事件"))
    logger.info("scheduler_event_start", title=title[:50])

    result = await run_single_event_conduction(event)
    logger.info(
        "scheduler_event_done",
        title=title[:50],
        success=result.success,
        event_generated=result.event_generated,
        persisted=result.persisted,
        error=result.error,
    )
