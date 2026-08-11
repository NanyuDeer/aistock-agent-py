"""迭代调度 —— 交易日 16:00 每日任务 + 手动触发。"""

import asyncio
import sys

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from aistock_agent.config import settings
from aistock_agent.iterate.case_builder import list_cases, load_case
from aistock_agent.iterate.reporter import run_daily_report
from aistock_agent.iterate.run_case import run_case
from aistock_agent.utils.date import is_trading_day, shanghai_today

logger = structlog.get_logger()


def register_iterate_jobs(scheduler: AsyncIOScheduler) -> None:
    """注册每日迭代汇总 job（id=iterate_daily）。"""
    scheduler.add_job(
        _run_iterate_daily_task,
        CronTrigger.from_crontab(
            settings.iterate_cron,
            timezone=settings.scheduler_timezone,  # 硬约束：显式 Asia/Shanghai
        ),
        id="iterate_daily",
        name="iterate closed-loop daily",
        replace_existing=True,
    )
    logger.info("iterate_job_registered", cron=settings.iterate_cron)


async def _run_iterate_daily_task() -> None:
    """每日迭代任务：非交易日跳过；生成当日切片 + 消费历史队列 + 发报告。"""
    today = shanghai_today()
    if not is_trading_day(today):
        logger.info("iterate_skip_non_trading_day", date=today.isoformat())
        return

    # ① 当日重大异动切片（首版：从已有切片队列消费；生成逻辑见扩展说明）
    # ② 消费历史案例（先进先出，每日最多 iterate_max_daily_cases 个）
    cases = list_cases()
    for case_id in cases[: settings.iterate_max_daily_cases]:
        case = load_case(case_id)
        try:
            await run_case(str(case["agent_id"]), case_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("iterate_case_failed", case_id=case_id, error=str(exc))

    # ④ 每日汇总报告（无重要结果也发）
    await run_daily_report()


async def _manual_once() -> None:
    await _run_iterate_daily_task()


def main(argv: list[str]) -> int:
    """手动触发：python -m aistock_agent.iterate.scheduler --once"""
    if "--once" not in argv:
        print("usage: python -m aistock_agent.iterate.scheduler --once", file=sys.stderr)
        return 2
    asyncio.run(_manual_once())
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
