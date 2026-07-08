"""定时调度服务 — APScheduler AsyncIOScheduler 集成

调度任务（均为交易日执行，非交易日自动跳过）：
  08:50  晨报生成（写Redis，用户打开App命中缓存）
  15:30  复盘生成（复盘 agent 实现后接入）
  15:35  快照生成（快照生成器实现后接入）
  15:40  迭代分析（迭代 agent 实现后接入）

集成方式：在 main.py lifespan 中 start_scheduler() / shutdown_scheduler()
"""

from datetime import date

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

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

    # 复盘生成：工作日 15:30（agent 实现后激活）
    scheduler.add_job(
        _run_review_task,
        CronTrigger.from_crontab(settings.scheduler_review_cron),
        id="review_report",
        name="复盘生成",
        replace_existing=True,
    )

    # 快照生成：工作日 15:35（快照生成器实现后激活）
    scheduler.add_job(
        _run_snapshot_task,
        CronTrigger.from_crontab(settings.scheduler_snapshot_cron),
        id="snapshot_build",
        name="快照生成",
        replace_existing=True,
    )

    # 迭代分析：工作日 15:40（迭代 agent 实现后激活）
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
    except Exception as e:
        logger.error("scheduler_morning_failed", error=str(e), exc_info=True)


async def _run_review_task() -> None:
    """复盘生成任务（交易日 15:30）— 复盘 agent 实现后激活"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="review")
        return

    logger.info("scheduler_review_start")
    # TODO: 复盘 agent 实现后接入
    # from aistock_agent.agents.workers.review import run as review_run
    # ...
    logger.info("scheduler_review_not_implemented_yet")


async def _run_snapshot_task() -> None:
    """快照生成任务（交易日 15:35）— 快照生成器实现后激活"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="snapshot")
        return

    logger.info("scheduler_snapshot_start")
    # TODO: 快照生成器实现后接入
    # from aistock_agent.services.snapshot_builder import build_snapshot
    # ...
    logger.info("scheduler_snapshot_not_implemented_yet")


async def _run_iterate_task() -> None:
    """迭代分析任务（交易日 15:40）— 迭代 agent 实现后激活"""
    if not is_trading_day():
        logger.info("scheduler_skip_non_trading_day", task="iterate")
        return

    logger.info("scheduler_iterate_start")
    # TODO: 迭代 agent 实现后接入
    # from aistock_agent.agents.workers.iterate import run as iterate_run
    # ...
    logger.info("scheduler_iterate_not_implemented_yet")
