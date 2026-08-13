"""iterate 调度 —— 注册 job 与手动触发"""

from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]


def test_register_iterate_jobs_adds_daily_job() -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from aistock_agent.iterate.scheduler import register_iterate_jobs

    scheduler = AsyncIOScheduler()
    register_iterate_jobs(scheduler)
    assert scheduler.get_job("iterate_daily") is not None


@pytest.mark.asyncio
async def test_run_iterate_daily_skips_non_trading_day() -> None:
    from aistock_agent.iterate.scheduler import _run_iterate_daily_task

    with patch(
        "aistock_agent.iterate.scheduler.is_trading_day", return_value=False
    ) as mock_td, patch(
        "aistock_agent.iterate.scheduler.run_daily_report", AsyncMock()
    ) as mock_report:
        await _run_iterate_daily_task()
    mock_td.assert_called_once()
    mock_report.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_iterate_daily_consumes_only_pending_cases(iterate_data_dir: object) -> None:
    """I4/D13 回归：已写 iterated.json 标记的案例不再被重复迭代（去重）。"""
    from aistock_agent.iterate.case_builder import list_pending_cases, mark_iterated
    from aistock_agent.iterate.scheduler import _run_iterate_daily_task

    case_id = "case_20260731_us_market_surge"
    mark_iterated(case_id)
    # Task 12 Fix Round：调度器消费的 pending 集合不得含 .iterated phantom id。
    # 排除逻辑被移除时，标记文件 stem（case_id.iterated）会冒充切片 id 混入 pending，
    # 且其 load_case 能命中标记文件自身、agent_id KeyError 又被 run_case 的 try/except
    # 吞掉 → mock_run 仍不被 await，既有断言静默通过——必须显式锁定此断言才有区分力。
    assert not any(cid.endswith(".iterated") for cid in list_pending_cases())
    with patch(
        "aistock_agent.iterate.scheduler.is_trading_day", return_value=True
    ), patch(
        "aistock_agent.iterate.scheduler.run_case", AsyncMock()
    ) as mock_run, patch(
        "aistock_agent.iterate.scheduler.run_daily_report", AsyncMock()
    ) as mock_report:
        await _run_iterate_daily_task()
    mock_run.assert_not_awaited()  # 该案例已有 iterated 标记 → 不再消费
    mock_report.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_iterate_daily_consumes_case_without_experiment(
    iterate_data_dir: object,
) -> None:
    """I4 回归：无实验记录的案例应被消费一次。"""
    from aistock_agent.iterate.scheduler import _run_iterate_daily_task

    with patch(
        "aistock_agent.iterate.scheduler.is_trading_day", return_value=True
    ), patch(
        "aistock_agent.iterate.scheduler.run_case", AsyncMock()
    ) as mock_run, patch(
        "aistock_agent.iterate.scheduler.run_daily_report", AsyncMock()
    ) as mock_report:
        await _run_iterate_daily_task()
    mock_run.assert_awaited_once()
    case = mock_run.await_args.args
    assert case[1] == "case_20260731_us_market_surge"
    mock_report.assert_awaited_once()


# ---- 产片接线 + 双 job 错峰（D16/F4/N6 修复）----


def test_register_iterate_jobs_registers_two_jobs() -> None:
    """注册两个 job：产片（16:30）+ 消费（17:00）。"""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from aistock_agent.config import settings
    from aistock_agent.iterate.scheduler import register_iterate_jobs

    scheduler = AsyncIOScheduler()
    register_iterate_jobs(scheduler)
    build_job = scheduler.get_job("iterate_case_build")
    assert build_job is not None
    assert scheduler.get_job("iterate_daily") is not None
    # APScheduler 3.x 的 CronTrigger/BaseField 未实现 __eq__，逐字段序列化
    # 表达式后与 settings.iterate_case_build_cron 重建的 trigger 比较。
    assert _cron_exprs(build_job.trigger) == _cron_exprs(
        CronTrigger.from_crontab(
            settings.iterate_case_build_cron, timezone=settings.scheduler_timezone
        )
    )


def _cron_exprs(trigger: CronTrigger) -> dict[str, str]:
    """把 CronTrigger 各字段序列化为 {字段名: 表达式}，供触发规则比对。"""
    return {f.name: ",".join(str(e) for e in f.expressions) for f in trigger.fields}


@pytest.mark.asyncio
async def test_build_task_failure_does_not_break_report(iterate_data_dir: object) -> None:
    """产片失败（Node 不可达）只告警，不中止每日迭代报告。"""
    from aistock_agent.iterate.scheduler import _run_iterate_build_task

    with patch(
        "aistock_agent.iterate.scheduler.find_recent_trading_day",
        AsyncMock(return_value=None),
    ), patch(
        "aistock_agent.iterate.scheduler._build_review_and_event_cases",
        AsyncMock(side_effect=RuntimeError("node unreachable")),
    ) as mock_build:
        await _run_iterate_build_task()
    mock_build.assert_awaited()
    # 产片失败不抛异常（被内部 try/except 吸收）
