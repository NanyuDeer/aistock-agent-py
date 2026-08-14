"""迭代调度 —— 工作日双 job：16:30 产片 + 17:00 消费/报告，另支持手动触发。"""

import asyncio
import sys
from datetime import date
from html import escape as html_escape

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from aistock_agent.config import settings
from aistock_agent.iterate.adapters import ITERABLE_AGENTS
from aistock_agent.iterate.case_builder import (
    get_data_dir,
    list_cases,
    list_pending_cases,
    load_case,
)
from aistock_agent.iterate.case_pipeline import build_cases_for_adapter
from aistock_agent.iterate.reporter import run_daily_report
from aistock_agent.iterate.run_case import run_case
from aistock_agent.utils.date import is_trading_day, shanghai_today

logger = structlog.get_logger()


def register_iterate_jobs(scheduler: AsyncIOScheduler) -> None:
    """注册每日产片（16:30）+ 消费/报告（17:00）两个 job。

    产片与消费拆分（D16 修复）：产片失败只跳过当日产片，不中止迭代报告；
    cron 错开 16:00 prediction_validate（F4 修复）。
    """
    scheduler.add_job(
        _run_iterate_build_task,
        CronTrigger.from_crontab(
            settings.iterate_case_build_cron,
            timezone=settings.scheduler_timezone,  # 硬约束：显式 Asia/Shanghai
        ),
        id="iterate_case_build",
        name="iterate case build (16:30)",
        replace_existing=True,
    )
    scheduler.add_job(
        _run_iterate_daily_task,
        CronTrigger.from_crontab(
            settings.iterate_cron,
            timezone=settings.scheduler_timezone,  # 硬约束：显式 Asia/Shanghai
        ),
        id="iterate_daily",
        name="iterate closed-loop daily (17:00)",
        replace_existing=True,
    )
    logger.info(
        "iterate_jobs_registered",
        build_cron=settings.iterate_case_build_cron,
        consume_cron=settings.iterate_cron,
    )


async def _run_iterate_build_task() -> None:
    """每日产片任务（16:30）：按 iterable_agent_ids 循环通用产片流水线（二期）。

    失败语义（D16/N6）：产片失败只跳过当日产片并告警，不中止迭代报告；
    每日任务（17:00）照常消费既有切片并发送报告。
    """
    today = shanghai_today()
    if not is_trading_day(today):
        logger.info("iterate_build_skip_non_trading_day", date=today.isoformat())
        return
    try:
        await produce_cases_daily()
    except Exception as exc:  # noqa: BLE001
        logger.error("iterate_case_build_failed", error=str(exc), exc_info=True)
        # D-3 修复：产片失败告警邮件（D16 语义：只跳过当日产片，不中止迭代报告）
        _notify_build_failure(today, exc)


def _notify_build_failure(report_date: date, exc: Exception) -> None:
    """产片失败告警邮件；配置缺失或发送失败仅记日志，不抛（不阻断闭环）。"""
    from aistock_agent.services.mail_sender import send_mail

    subject = f"迭代产片失败告警 {report_date.isoformat()}"
    body_html = (
        "<pre style='font-family:Menlo,Consolas,monospace;font-size:12px;'>"
        f"迭代产片任务失败（{report_date.isoformat()}）：\n\n"
        f"{html_escape(str(exc))}\n\n"
        "今日切片可能缺失；17:00 迭代报告照常消费既有切片。</pre>"
    )
    try:
        ok = send_mail(subject, body_html)
        if not ok:
            logger.warning("iterate_build_failure_mail_not_sent", subject=subject)
    except Exception as mail_exc:  # noqa: BLE001 — 告警失败不阻断
        logger.warning("iterate_build_failure_mail_error", error=str(mail_exc))


async def produce_cases_daily() -> dict[str, object]:
    """每日产片：按 iterable_agent_ids 循环通用流水线（二期）。

    单 agent 失败 → 告警邮件（D-3）+ 记录，不阻断其他 agent。
    返回 {agent_id: BuildResult}；未声明 case_sources 的 adapter 跳过。

    为什么模块级 import（而非函数内 lazy import）：测试/调用方需经
    ``scheduler.build_cases_for_adapter`` 打桩隔离，函数内 import 会绑定
    局部名导致 monkeypatch 失效（Task 5 评审发现，与旧的 lazy import
    模式刻意不同——旧模式引入的 scripts 断链已修复）。
    """
    results: dict[str, object] = {}
    for agent_id, adapter in ITERABLE_AGENTS.items():
        if not adapter.case_sources:
            continue  # 未声明产片源的 adapter 不参与产片
        try:
            results[agent_id] = await build_cases_for_adapter(
                adapter, data_dir=get_data_dir()
            )
        except Exception as exc:  # noqa: BLE001 — 单 agent 失败不阻断
            logger.error(
                "iterate_produce_cases_failed",
                agent_id=agent_id,
                error=str(exc),
                exc_info=True,
            )
            _notify_build_failure(shanghai_today(), exc)
            results[agent_id] = {"error": str(exc)}
    logger.info("iterate_cases_built", summary=str(results))
    return results


async def _run_iterate_daily_task(report_date: date | None = None) -> None:
    """每日迭代任务：非交易日跳过；消费待迭代案例 + 发报告。

    切片生成由 16:30 产片 job（_run_iterate_build_task）负责（D16 修复），
    本任务只消费既有切片（data/cases/）并发送报告，产片失败不阻断消费。
    案例去重（I4，D13 修复）：只消费尚未迭代的切片——已迭代判定基于
    ``data/cases/{case_id}.iterated.json`` 标记文件（不再看 experiments ``_r``
    前缀），每个案例只迭代一次，避免每个交易日反复重跑最新 N 个案例。
    report_date：手动补发历史日期报告（--once --date），默认当日。
    """
    today = shanghai_today()
    if not is_trading_day(today):
        logger.info("iterate_skip_non_trading_day", date=today.isoformat())
        return

    # D13 修复：每日任务开始前执行一次性迁移（幂等，存量 experiments 前缀 → 标记文件）
    from aistock_agent.iterate.case_builder import migrate_iterated_marks

    migrated = migrate_iterated_marks()
    if migrated:
        logger.info("iterate_iterated_marks_migrated", count=migrated)

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
    await run_daily_report(report_date)


def _parse_date_arg(argv: list[str]) -> date | None:
    """解析 --date YYYY-MM-DD（--once 补发模式用）；缺失/非法返回 None。"""
    if "--date" not in argv:
        return None
    idx = argv.index("--date")
    if idx + 1 >= len(argv):
        return None
    try:
        return date.fromisoformat(argv[idx + 1])
    except ValueError:
        return None


async def _manual_once(argv: list[str]) -> None:
    report_date = _parse_date_arg(argv)
    if report_date is not None:
        # 补发模式：仅构建并发送指定日期报告（跳过案例消费与交易日检查）
        logger.info("iterate_report_resend", report_date=report_date.isoformat())
        await run_daily_report(report_date)
        return
    await _run_iterate_daily_task()


def main(argv: list[str]) -> int:
    """手动触发：python -m aistock_agent.iterate.scheduler --once [--date YYYY-MM-DD]"""
    if "--once" not in argv:
        print(
            "usage: python -m aistock_agent.iterate.scheduler "
            "--once [--date YYYY-MM-DD]",
            file=sys.stderr,
        )
        return 2
    asyncio.run(_manual_once(argv))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
