"""迭代调度 —— 交易日 16:00 每日任务 + 手动触发。"""

import asyncio
import sys

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from aistock_agent.config import settings
from aistock_agent.iterate.case_builder import list_cases, list_pending_cases, load_case
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
    """每日迭代任务：非交易日跳过；消费待迭代案例 + 发报告。

    切片生成（build_case）明确列为二期，首版只消费沙盒内既有切片——
    沙盒 data/cases/ 由人工/外部流程预置，本任务不做当日切片生成。
    案例去重（I4）：只消费尚无实验记录的切片（data/experiments/ 下无
    ``{case_id}_r`` 前缀文件），每个案例只迭代一次，避免每个交易日
    反复重跑最新 N 个案例。
    """
    today = shanghai_today()
    if not is_trading_day(today):
        logger.info("iterate_skip_non_trading_day", date=today.isoformat())
        return

    # 消费历史案例（先进先出，每日最多 iterate_max_daily_cases 个；只消费未迭代过的）
    pending = list_pending_cases()
    if not pending:
        logger.info("iterate_no_pending_cases", case_count=len(list_cases()))
    for case_id in pending[: settings.iterate_max_daily_cases]:
        case = load_case(case_id)
        try:
            await run_case(str(case["agent_id"]), case_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("iterate_case_failed", case_id=case_id, error=str(exc))

    # 每日汇总报告（无重要结果也发；空案例库时报告会注明"无待迭代案例"）
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
