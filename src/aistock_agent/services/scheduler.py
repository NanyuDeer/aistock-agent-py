"""定时任务调度器（APScheduler）

在进程启动时自动开启，通过 AsyncIOScheduler 管理所有定时任务：
- 08:50 晨报 analysis：morning agent（宏观策略4步框架，缓存+落盘）
  → 完成后自动提取 major_events，并行触发 event agent 传导分析（fire-and-forget）
- 09:00 播报链路 broadcast：串行执行 morning→wind_leader→hot_burst→broadcast（报告写DB+双人语音，9:10前端可见）
- 15:30 复盘 review：review agent（5步归因框架，缓存+落盘）
- 15:35 快照 snapshot：build_snapshot（代码层匹配 + LLM 4维评估 → 落盘 JSON）
- 15:40 迭代分析 iterate：iterate agent（硬编码阈值 + LLM 偏差分析 → JSON 输出）
"""

import asyncio
from datetime import date

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from aistock_agent.config import settings
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import is_trading_day

logger = structlog.get_logger()

_scheduler: AsyncIOScheduler | None = None


def get_scheduler() -> AsyncIOScheduler:
    """获取调度器单例（懒初始化，未 start 前仅创建实例）"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    return _scheduler


def start_scheduler() -> None:
    """启动调度器，注册所有定时任务。

    若 settings.scheduler_enabled 为 False 则跳过（开发/测试环境可关闭）。
    重复调用安全：add_job 使用 replace_existing=True 覆盖同 id 任务。
    """
    if not settings.scheduler_enabled:
        logger.info("scheduler_disabled_by_config")
        return

    scheduler = get_scheduler()

    # 晨报生成：工作日 08:50
    scheduler.add_job(
        _run_morning_task,
        CronTrigger.from_crontab(settings.scheduler_morning_cron),
        id="morning_briefing",
        name="晨报生成",
        replace_existing=True,
    )

    # 播报链路：工作日 09:00（串行 morning→wind_leader→hot_burst→broadcast，9:10前端可见）
    scheduler.add_job(
        _run_broadcast_task,
        CronTrigger.from_crontab(settings.scheduler_broadcast_cron),
        id="broadcast_chain",
        name="播报链路",
        replace_existing=True,
    )

    # 复盘生成：工作日 15:30
    scheduler.add_job(
        _run_review_task,
        CronTrigger.from_crontab(settings.scheduler_review_cron),
        id="review_report",
        name="复盘生成",
        replace_existing=True,
    )

    # 快照生成：工作日 15:35
    scheduler.add_job(
        _run_snapshot_task,
        CronTrigger.from_crontab(settings.scheduler_snapshot_cron),
        id="snapshot_build",
        name="快照生成",
        replace_existing=True,
    )

    # 迭代分析：工作日 15:40
    scheduler.add_job(
        _run_iterate_task,
        CronTrigger.from_crontab(settings.scheduler_iterate_cron),
        id="iterate_analysis",
        name="迭代分析",
        replace_existing=True,
    )

    scheduler.start()
    logger.info("scheduler_started", jobs=[j.id for j in scheduler.get_jobs()])


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
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="morning")
        return

    logger.info("scheduler_morning_start")
    # 函数内 import：延迟加载 LangGraph/LangChain 重依赖，避免 scheduler 模块加载时拖慢启动
    from aistock_agent.agents.workers import morning as morning_agent

    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_morning_{date.today().isoformat()}",
        "user_id": None,
        "favorites": [],
        "intent": "morning",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
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
        major_events: list[dict[str, object]] = result.get("analysis_reports", {}).get("major_events", [])  # type: ignore[assignment]
        if major_events:
            event_tasks = [
                asyncio.create_task(_run_event_task(event))
                for event in major_events
                if isinstance(event, dict) and event.get("title")
            ]
            logger.info(
                "scheduler_event_triggered",
                total=len(event_tasks),
                titles=[e.get("title", "")[:30] for e in major_events if isinstance(e, dict)],
            )
            # fire-and-forget: 各个事件 task 独立在后台运行，错误由 _run_event_task 内部捕获
    except Exception as e:
        logger.error("scheduler_morning_failed", error=str(e), exc_info=True)


async def _run_review_task() -> None:
    """复盘生成任务（交易日 15:30）"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="review")
        return

    logger.info("scheduler_review_start")
    from aistock_agent.agents.workers import review as review_agent

    state: AgentState = {
        "messages": [],
        "session_id": f"scheduled_review_{date.today().isoformat()}",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
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
    if not is_trading_day():
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
    if not is_trading_day():
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

    串行执行：morning → wind_leader → hot_burst → broadcast。
    每个 Agent 设置 trigger_source="scheduler" + report_date，使报告写入数据库。
    broadcast 从数据库读取报告生成双人语音播报。

    每个 Agent 异常独立捕获，不影响后续 Agent 执行。
    morning 在 08:50 已执行过，此处命中缓存快速返回（或重新生成）。
    """
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="broadcast")
        return

    today = date.today().isoformat()
    logger.info("scheduler_broadcast_chain_start", report_date=today)

    # 函数内 import：延迟加载重依赖
    from aistock_agent.agents.workers import broadcast as broadcast_agent
    from aistock_agent.agents.workers import hot_burst as hot_burst_agent
    from aistock_agent.agents.workers import morning as morning_agent
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

    # Step 4: 播报生成（从数据库读取报告）
    try:
        broadcast_result = await broadcast_agent.run(_make_state())
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

    Args:
        event: major_event dict，含 title/summary/url/impact_score/direction/involved_keywords
    """
    title = str(event.get("title", "未知事件"))
    summary = str(event.get("summary", ""))
    url = str(event.get("url", ""))

    logger.info("scheduler_event_start", title=title[:50])

    # 构建事件分析的用户消息
    user_message = f"请分析以下重大事件：{title}"
    if summary:
        user_message += f"\n\n事件概述：{summary}"
    if url:
        user_message += f"\n\n原文链接：{url}"

    state: AgentState = {
        "messages": [{"role": "user", "content": user_message}],
        "session_id": f"scheduled_event_{date.today().isoformat()}_{title[:20]}",
        "user_id": None,
        "favorites": [],
        "intent": "event",
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }

    try:
        from aistock_agent.agents.workers import event as event_agent

        result = await event_agent.run(state)
        logger.info(
            "scheduler_event_done",
            title=title[:50],
            has_response=bool(result.get("final_response")),
            has_display_report=bool(
                result.get("analysis_reports", {}).get("event_display_report")
            ),
        )
    except Exception as e:
        logger.error(
            "scheduler_event_failed",
            title=title[:50],
            error=str(e),
            exc_info=True,
        )
