"""定时任务调度器（APScheduler）

在进程启动时自动开启，通过 AsyncIOScheduler 管理所有定时任务：
- 08:50 晨报 analysis：morning agent（宏观策略4步框架，缓存+落盘）
  （2026-08-12 起事件传导触发迁移到统一事件抓取中台 event_scrape 入库后，
  见 Task 5；晨报仅在"（事件库为空 或 无当日传导报告）且未被中台标记"时降级
  兜底触发 _run_event_analysis_pipeline_task，防中台抓取全失败时传导静默缺失，见 I4/H7。）
- 09:00 播报链路 broadcast：串行执行 morning→wind_leader→hot_burst→trend_score→broadcast
  （不依赖 event_conduction / global_importance）
- 15:30 晚间链路：review → market_snapshot → iterate → Brief → broadcast
"""

import asyncio
import json
import time
from datetime import date
from typing import Any

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

# H3（2026-08-24）：盘中报 12:05 AI 段与 event_scrape 串行化的最小实现。
# 单进程单 loop（uvicorn），Semaphore(1) 足够；未来多 worker 需换 Redis 分布式锁。
_midday_llm_semaphore = asyncio.Semaphore(1)

_PERSISTABLE_ITERATE_STATUSES = frozenset({"normal", "alert"})

_scheduler: AsyncIOScheduler | None = None


async def _scheduler_heartbeat() -> None:
    """调度器心跳（诊断）：验证运行进程内 cron 调度链路是否存活。

    每 2 分钟触发一次并打印日志；若长时间无 heartbeat，
    说明 AsyncIOScheduler 在 uvicorn 进程内未按时调度（misfire 或
    event loop 阻塞），可据此定位定时任务不触发问题。
    """
    logger.info(
        "scheduler_heartbeat",
        jobs=[job.id for job in get_scheduler().get_jobs()],
        report_date=shanghai_today().isoformat(),
    )


def get_scheduler() -> AsyncIOScheduler:
    """获取调度器单例（懒初始化，未 start 前仅创建实例）"""
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone=settings.scheduler_timezone)
    return _scheduler


def start_scheduler() -> None:
    """注册早报、早间播报与晚间链路。

    quick_snapshot_enabled=true 时注册 review_quick/review_full 两个 cron，
    并启动事件消费者；false 时保留旧 _run_evening_chain_task 串行链路。
    """
    if settings.qa_mode_enabled:
        logger.info("scheduler_disabled_by_qa_mode")
        return
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
        _run_midday_task,
        CronTrigger.from_crontab(
            settings.scheduler_midday_cron,
            timezone=settings.scheduler_timezone,
        ),
        id="midday_briefing",
        name="midday briefing",
        replace_existing=True,
    )
    # 午报播报（音频/双人）：12:15 错峰于 12:05 midday 落库之后（H3 信号量），回填 audio_path
    scheduler.add_job(
        _run_midday_broadcast_task,
        CronTrigger.from_crontab(
            settings.scheduler_midday_broadcast_cron,
            timezone=settings.scheduler_timezone,
        ),
        id="midday_broadcast",
        name="midday broadcast",
        replace_existing=True,
    )
    # ── 统一事件抓取中台（2026-08-12；2026-08-13 盘前全量 07:30→08:45，盘中 12:00 恢复） ──
    # 盘前档（08:45 全量，紧邻晨报 08:50 前完成；原 07:30 太早，早间公告未出
    # 全量价值低，故合并）与盘中档（10-14 点每小时增量，含 12:00 午间档——
    # 午休期间仍有午间公告/新闻发布，2026-08-13 用户裁决恢复）；收盘汇总
    # 15:05 复用 full_daily，供复盘/播报消费。
    scheduler.add_job(
        _run_event_scrape_job,
        CronTrigger.from_crontab(
            settings.scheduler_event_scrape_cron,
            timezone=settings.scheduler_timezone,
        ),
        kwargs={"scrape_mode": "full_daily"},
        id="event_scrape_daily",
        name="event scrape daily",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    scheduler.add_job(
        _run_event_scrape_job,
        CronTrigger.from_crontab(
            settings.scheduler_event_scrape_intraday_cron,
            timezone=settings.scheduler_timezone,
        ),
        kwargs={"scrape_mode": "intraday"},
        id="event_scrape_intraday",
        name="event scrape intraday",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    # 收盘汇总（15:05）：全天事件汇总补抓，供复盘/播报消费（H5，2026-08-13）
    scheduler.add_job(
        _run_event_scrape_job,
        CronTrigger.from_crontab(
            settings.scheduler_event_scrape_close_cron,
            timezone=settings.scheduler_timezone,
        ),
        kwargs={"scrape_mode": "full_daily"},
        id="event_scrape_close",
        name="event scrape close summary",
        replace_existing=True,
        misfire_grace_time=3600,
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
        _run_prediction_validate_task,
        CronTrigger.from_crontab(
            settings.scheduler_prediction_validate_cron,
            timezone=settings.scheduler_timezone,
        ),
        id="prediction_validate",
        name="prediction due validation",
        replace_existing=True,
    )
    # 预测验证统计出口（D3，独立调度与验证解耦）：16:05 验证落库后汇总命中率/baseline
    scheduler.add_job(
        _run_prediction_stats_task,
        CronTrigger.from_crontab(
            settings.scheduler_prediction_stats_cron,
            timezone=settings.scheduler_timezone,
        ),
        id="prediction_stats",
        name="prediction hit-rate stats",
        replace_existing=True,
    )

    if settings.quick_snapshot_enabled:
        # 新事件驱动链路：review_quick(15:30) + review_full(20:30)
        scheduler.add_job(
            _publish_review_quick_event,
            CronTrigger.from_crontab(
                settings.scheduler_review_quick_cron,
                timezone=settings.scheduler_timezone,
            ),
            id="review_quick",
            name="quick review (event-driven)",
            replace_existing=True,
        )
        scheduler.add_job(
            _publish_review_full_event,
            CronTrigger.from_crontab(
                settings.scheduler_review_full_cron,
                timezone=settings.scheduler_timezone,
            ),
            id="review_full",
            name="full review (event-driven)",
            replace_existing=True,
        )
        logger.info("scheduler_quick_snapshot_enabled")
    else:
        # 旧串行链路（保留向后兼容）
        scheduler.add_job(
            _run_evening_chain_task,
            CronTrigger.from_crontab(
                settings.scheduler_review_cron,
                timezone=settings.scheduler_timezone,
            ),
            id="evening_chain",
            name="evening report chain (legacy)",
            replace_existing=True,
        )
        logger.info("scheduler_legacy_evening_chain")

    if settings.iterate_enabled:
        from aistock_agent.iterate.scheduler import register_iterate_jobs

        register_iterate_jobs(scheduler)
        logger.info("iterate_jobs_registered_via_main_scheduler")

    # 触发时刻 event loop 若短暂繁忙，APScheduler 默认 misfire_grace_time=1s
    # 会直接跳过任务（表现为"cron 未触发"且无任何日志）。放宽到 1 小时，
    # 并在 loop 恢复后补跑；coalesce=False 保留每次错过的触发。
    for job in scheduler.get_jobs():
        job.misfire_grace_time = 3600
        job.coalesce = False

    # 心跳 job：每 2 分钟验证调度链路存活（诊断定时任务不触发问题）
    scheduler.add_job(
        _scheduler_heartbeat,
        CronTrigger.from_crontab(
            "*/2 * * * *",
            timezone=settings.scheduler_timezone,
        ),
        id="scheduler_heartbeat",
        name="scheduler heartbeat",
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

# 保持 fire-and-forget 传导 task 的强引用，避免被 GC 回收导致事件传导分析静默丢失
# （Task 4 删除晨报直接触发后该集合一并移除；I4 兜底分支恢复使用，AGENTS.md
# 明确警告 "fire-and-forget task 若不保存引用会被 GC 在执行前取消"）。
_pending_event_tasks: set[asyncio.Task[Any]] = set()


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
        # 事件传导触发已迁移到统一事件抓取中台（2026-08-12）：
        # Task 5 起由 scrape_full_daily / scrape_intraday 入库成功后触发。
        # I4 兜底（中台抓取失败时的安全网）：当日（事件库为空 或 无当日传导报告）
        # 且未被中台标记（conduction_triggered:{date} 不存在）时，若晨报 LLM 仍
        # 识别出 major_events（自主检索兜底产出），降级 fire-and-forget 触发
        # 事件传导——恢复 Task 4 删除晨报触发前的语义，避免"当日抓取全部失败
        # → 传导静默缺失且无告警"。H7（2026-08-13）：中台触发传导即写当日标记，
        # 晨报检查标记避免与中台双跑；传导报告已落库（has_conduction）同样抑制。
        analysis_reports = result.get("analysis_reports")
        raw_major_events = (
            analysis_reports.get("major_events", [])
            if isinstance(analysis_reports, dict)
            else []
        )
        major_events: list[dict[str, object]] = (
            [event for event in raw_major_events if isinstance(event, dict)]
            if isinstance(raw_major_events, list)
            else []
        )
        if major_events:
            from aistock_agent.services.event_store import (  # noqa: PLC0415
                load_event_scrape,
            )
            from aistock_agent.services.redis_pool import RedisPool  # noqa: PLC0415

            event_store_events = await load_event_scrape(report_date)
            # I4 兜底放宽（H7，2026-08-13）：库空 或 库有数据但当日无传导报告，
            # 且当日未被中台标记触发过 → 晨报降级触发（防"抓取成功但传导失败"静默缺失）。
            # 中台触发时会设置 conduction_triggered:{date} 标记，此处检查避免双跑。
            triggered = False
            try:
                client = await RedisPool.get_client()
                triggered = bool(await client.get(f"conduction_triggered:{report_date}"))
            except Exception:  # noqa: BLE001
                logger.debug("morning_conduction_mark_check_failed", date=report_date)
            has_conduction = bool(
                await node_api.list_analysis_reports("event_conduction", report_date)
            )
            if (not event_store_events or not has_conduction) and not triggered:
                logger.warning(
                    "morning_conduction_fallback_triggered",
                    event_store_empty=not bool(event_store_events),
                    has_conduction=has_conduction,
                    event_count=len(major_events),
                )
                task = asyncio.create_task(
                    _run_event_analysis_pipeline_task(major_events)
                )
                _pending_event_tasks.add(task)
                task.add_done_callback(_pending_event_tasks.discard)
    except Exception as e:
        logger.error("scheduler_morning_failed", error=str(e), exc_info=True)


async def _run_midday_task(report_date: str | None = None) -> dict[str, object]:
    """盘中报生成任务（工作日 12:05）。

    交易日守卫 + H3 信号量串行化（与 event_scrape AI 段错峰） + 调用 midday.run。
    report_date 缺省时使用上海当天做交易日检查；显式传入（YYYY-MM-DD）时同样
    进行交易日校验，避免手动补跑时绕过守卫（controller 裁决，替代计划原始
    "显式传入跳过检查"语义）。

    返回状态 dict 供手动触发端点透传：
    - skipped(non_trading_day) / ok / partial / failed
    """
    day = shanghai_today()
    if report_date is not None:
        day = date.fromisoformat(report_date)
    if not is_trading_day(day):
        logger.info("scheduler_skip_non_trading_day", task="midday")
        return {"status": "skipped", "reason": "non_trading_day"}
    report_date = day.isoformat()

    logger.info("scheduler_midday_start", report_date=report_date)
    from aistock_agent.agents.workers import midday as midday_agent

    state = _make_scheduled_state(report_date, intent="midday")

    async with _midday_llm_semaphore:
        try:
            result = await midday_agent.run(state)
            generated = bool(
                result.get("analysis_reports", {}).get("midday_generated")
            ) if isinstance(result.get("analysis_reports"), dict) else False
            persisted = bool(
                result.get("analysis_reports", {}).get("midday_persisted")
            ) if isinstance(result.get("analysis_reports"), dict) else False
            logger.info(
                "scheduler_midday_done",
                report_date=report_date,
                generated=generated,
                persisted=persisted,
            )
            return {"status": "ok" if generated else "partial", "report_date": report_date}
        except Exception as e:
            # run() 已内层 try-catch；此处兜底
            logger.error("scheduler_midday_failed", error=str(e), exc_info=True)
            return {"status": "failed", "reason": str(e), "report_date": report_date}


async def _run_midday_broadcast_task(report_date: str | None = None) -> dict[str, object]:
    """午报播报生成任务（工作日 12:15）。

    错峰于 12:05 ``_run_midday_task`` 落库之后；持 ``_midday_llm_semaphore``（H3）
    使其对话 LLM 段不与 event_scrape 抢算力。调用 ``midday_broadcast.run``，
    成功后 midday 报告 ``content.audio_path`` 已被回填。
    """
    day = shanghai_today()
    if report_date is not None:
        try:
            day = date.fromisoformat(report_date)
        except ValueError:
            day = shanghai_today()
    if not is_trading_day(day):
        logger.info("scheduler_skip_non_trading_day", task="midday_broadcast")
        return {"status": "skipped", "reason": "non_trading_day"}
    report_date = day.isoformat()

    logger.info("scheduler_midday_broadcast_start", report_date=report_date)
    from aistock_agent.agents.workers import midday_broadcast as midday_broadcast_agent

    state = _make_scheduled_state(report_date, intent="midday_broadcast")

    async with _midday_llm_semaphore:
        try:
            result = await midday_broadcast_agent.run(state)
            mb_info = result.get("midday_broadcast")
            generated = (
                bool(mb_info.get("generated")) if isinstance(mb_info, dict) else False
            )
            audio_path = (
                mb_info.get("audio_path") if isinstance(mb_info, dict) else None
            )
            logger.info(
                "scheduler_midday_broadcast_done",
                report_date=report_date,
                generated=generated,
                has_audio=bool(audio_path),
            )
            return {
                "status": "ok" if generated else "partial",
                "report_date": report_date,
            }
        except Exception as e:
            # run() 已内层 try-catch；此处兜底
            logger.error("scheduler_midday_broadcast_failed", error=str(e), exc_info=True)
            return {"status": "failed", "reason": str(e), "report_date": report_date}


async def _run_event_analysis_pipeline_task(major_events: list[dict[str, object]]) -> None:
    """后台运行事件分析流水线（fire-and-forget 包装）。

    委托给 services/event_analysis_pipeline.run_event_analysis_pipeline，
    pipeline 内部完成 Event Conduction → Global Importance 全链路，
    并自行处理超时/异常（不向上抛）。

    调用方：I4 兜底（_run_morning_task 事件库为空时）与中台复用
    （event_scraper 入库后触发；M1：Task 4 曾删除晨报触发使其无生产调用方，
    I4 兜底恢复后重新被调用）。
    """
    from aistock_agent.services.event_analysis_pipeline import (  # noqa: PLC0415
        run_event_analysis_pipeline,
    )

    logger.info("scheduler_event_pipeline_start", event_count=len(major_events))
    await run_event_analysis_pipeline(major_events)
    logger.info("scheduler_event_pipeline_done", event_count=len(major_events))


async def _run_event_scrape_job(scrape_mode: str) -> None:
    """定时执行事件抓取（交易日守卫 + fire-and-forget）。"""
    from aistock_agent.services.event_scraper import run_event_scrape

    try:
        # 上海时区自然日（对齐 _run_morning_task；date.today() 用服务器本地时区，
        # 跨时区部署时与交易日判定/score_date 语义不一致）
        report_day = shanghai_today()
        today = report_day.isoformat()
        if not is_trading_day(report_day):
            logger.info("event_scrape_skipped_non_trading_day", date=today)
            return
        result = await run_event_scrape(scrape_mode, score_date=today)
        # result 已含 scrape_mode 键（run_event_scrape 返回 {"scrape_mode", ...}），
        # 重复传 scrape_mode= 会抛 TypeError，被下方 except 吞成误报的 job_failed
        logger.info("event_scrape_job_done", **result)
    except Exception as exc:  # noqa: BLE001
        logger.exception("event_scrape_job_failed", scrape_mode=scrape_mode, error=str(exc))


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


async def _run_evening_chain_task(report_date: str | None = None) -> dict[str, object]:
    """串行生成 review、market_snapshot、iterate、Brief 与晚间播报。

    report_date 缺省时使用上海当天并做交易日检查；显式传入时视为管理员
    手动补跑（/admin/trigger/evening_chain），跳过交易日检查。
    返回各阶段状态 dict，供手动触发端点透传给调用方。
    """
    if report_date is None:
        report_day = shanghai_today()
        if not is_trading_day(report_day):
            logger.info("scheduler_skip_non_trading_day", task="evening_chain")
            return {"status": "skipped", "reason": "non_trading_day"}
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
            return {
                "status": "failed",
                "stage": "review",
                "error": "review report missing or incomplete",
            }
    except Exception as exc:
        logger.error("scheduler_evening_review_failed", error=str(exc), exc_info=True)
        return {"status": "failed", "stage": "review", "error": str(exc)}

    try:
        snapshot = await asyncio.to_thread(build_snapshot, report_date)
        if not isinstance(snapshot, dict) or snapshot.get("error"):
            error = (
                snapshot.get("error") if isinstance(snapshot, dict) else "invalid_payload"
            )
            logger.error(
                "scheduler_evening_snapshot_invalid",
                report_date=report_date,
                error=error,
            )
            return {"status": "failed", "stage": "market_snapshot", "error": error}

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
            return {
                "status": "failed",
                "stage": "market_snapshot",
                "error": "market_snapshot not traceable",
            }
    except Exception as exc:
        logger.error("scheduler_evening_snapshot_failed", error=str(exc), exc_info=True)
        return {"status": "failed", "stage": "market_snapshot", "error": str(exc)}

    try:
        iterate_result = await iterate_agent.run(_make_scheduled_state(report_date))
        iterate_text = str(iterate_result.get("final_response") or "")
        iterate_payload = json.loads(iterate_text)
        if (
            not isinstance(iterate_payload, dict)
            or iterate_payload.get("status") not in _PERSISTABLE_ITERATE_STATUSES
        ):
            logger.error("scheduler_evening_iterate_invalid", report_date=report_date)
            return {
                "status": "failed",
                "stage": "iterate",
                "error": "iterate payload invalid or not persistable",
            }

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
            return {"status": "failed", "stage": "iterate", "error": "iterate not traceable"}
    except Exception as exc:
        logger.error("scheduler_evening_iterate_failed", error=str(exc), exc_info=True)
        return {"status": "failed", "stage": "iterate", "error": str(exc)}

    brief_saved = False
    try:
        brief_saved = await build_and_persist_brief("evening", report_date)
    except Exception as exc:
        logger.error("scheduler_evening_brief_failed", error=str(exc), exc_info=True)

    broadcast_ok = False
    if brief_saved:
        try:
            await broadcast_agent.run(_make_scheduled_state(report_date, brief_type="evening"))
            broadcast_ok = True
        except Exception as exc:
            logger.error("scheduler_evening_broadcast_failed", error=str(exc), exc_info=True)

    return {
        "status": "ok" if brief_saved else "partial",
        "report_date": report_date,
        "stages": {
            "review": "ok",
            "market_snapshot": "ok",
            "iterate": "ok",
            "brief": "ok" if brief_saved else "failed",
            "broadcast": "ok" if broadcast_ok else ("skipped" if not brief_saved else "failed"),
        },
    }


# ─── 事件驱动：EventBus 发布函数 ───


async def _publish_review_quick_event() -> None:
    """15:30 cron 触发：发布 review_quick 事件到 EventBus。"""
    report_day = shanghai_today()
    if not is_trading_day(report_day):
        logger.info("scheduler_skip_non_trading_day", task="review_quick")
        return

    report_date = report_day.isoformat()
    # 用单调时钟而非 asyncio.get_event_loop().time()：不隐式依赖"当前事件循环"
    # （同步测试/多线程场景下可能无 loop 而抛 RuntimeError，trace_id 只需时间戳）
    trace_id = f"sched-quick-{report_date}-{int(time.monotonic())}"

    try:
        event_bus = await _get_event_bus()
        if event_bus is None:
            logger.error("scheduler_event_bus_unavailable", task="review_quick")
            return
        await event_bus.publish(
            "review_quick",
            payload={"report_date": report_date, "trace_id": trace_id},
            event_id=f"review_quick_{report_date}",
        )
        logger.info("scheduler_review_quick_published", report_date=report_date, trace_id=trace_id)
    except Exception as exc:
        logger.error("scheduler_review_quick_publish_failed", error=str(exc), exc_info=True)


async def _publish_review_full_event() -> None:
    """20:30 cron 触发：发布 review_full 事件到 EventBus。"""
    report_day = shanghai_today()
    if not is_trading_day(report_day):
        logger.info("scheduler_skip_non_trading_day", task="review_full")
        return

    report_date = report_day.isoformat()
    trace_id = f"sched-full-{report_date}-{int(time.monotonic())}"

    try:
        event_bus = await _get_event_bus()
        if event_bus is None:
            logger.error("scheduler_event_bus_unavailable", task="review_full")
            return
        await event_bus.publish(
            "review_full",
            payload={"report_date": report_date, "trace_id": trace_id},
            event_id=f"review_full_{report_date}",
        )
        logger.info("scheduler_review_full_published", report_date=report_date, trace_id=trace_id)
    except Exception as exc:
        logger.error("scheduler_review_full_publish_failed", error=str(exc), exc_info=True)


async def _get_event_bus():
    """获取全局 EventBus 实例（由 main.py lifespan 初始化）。"""
    from aistock_agent.services.event_bus import EventBus
    from aistock_agent.services.redis_pool import RedisPool

    try:
        redis = await RedisPool.get_client()
        return EventBus(
            redis,
            max_retries=settings.event_bus_max_retries,
            deadletter_prefix=settings.event_bus_deadletter_prefix,
            consumer_group=settings.event_bus_consumer_group,
            stream_max_len=settings.event_stream_max_len,
        )
    except RuntimeError:
        return None


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
    本链路不依赖 event_conduction / global_importance：
    Global Importance 由 08:50 morning 触发的事件分析流水线
    （event_analysis_pipeline）在事件传导完成后生成。

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


async def _run_prediction_validate_task() -> None:
    """预测到期验证任务（交易日 16:00，收盘后）。"""
    if not is_trading_day(shanghai_today()):
        logger.info("scheduler_skip_non_trading_day", task="prediction_validate")
        return
    from aistock_agent.services.prediction_validator import run_once  # noqa: PLC0415

    try:
        updated = await run_once()
        logger.info("scheduler_prediction_validate_done", updated=updated)
    except Exception as e:
        logger.error("scheduler_prediction_validate_failed", error=str(e), exc_info=True)


async def _run_prediction_stats_task() -> None:
    """预测验证统计出口（D3，交易日 16:05 独立调度，与验证解耦）。"""
    if not is_trading_day(shanghai_today()):
        logger.info("scheduler_skip_non_trading_day", task="prediction_stats")
        return
    from aistock_agent.services.prediction_validator import _report_stats  # noqa: PLC0415

    try:
        await _report_stats()
        logger.info("scheduler_prediction_stats_done")
    except Exception as e:
        logger.error("scheduler_prediction_stats_failed", error=str(e), exc_info=True)

