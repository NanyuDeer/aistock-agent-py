"""iterate 调度 —— 注册 job 与手动触发"""

from datetime import date
from unittest.mock import AsyncMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]


@pytest.mark.asyncio
async def test_manual_once_with_date_sends_report_only() -> None:
    """--once --date 补发模式：仅构建并发送指定日期报告，跳过消费与交易日检查。

    2026-08-14 用户需求：迭代报告已生成但主应用 scheduler 未运行（无自动发送），
    需手动触发补发。--date 允许补发历史日期的报告（含当日实验记录）。
    """
    from aistock_agent.iterate.scheduler import _manual_once

    with patch(
        "aistock_agent.iterate.scheduler.run_daily_report", AsyncMock()
    ) as mock_report, patch(
        "aistock_agent.iterate.scheduler._run_iterate_daily_task", AsyncMock()
    ) as mock_daily:
        await _manual_once(["--once", "--date", "2026-08-13"])

    mock_report.assert_awaited_once_with(date(2026, 8, 13))
    mock_daily.assert_not_awaited()  # 补发模式不消费案例、不受交易日限制


@pytest.mark.asyncio
async def test_manual_once_without_date_runs_daily_task() -> None:
    """--once 无 --date：走完整每日任务（消费 + 当日报告）。"""
    from aistock_agent.iterate.scheduler import _manual_once

    with patch(
        "aistock_agent.iterate.scheduler.run_daily_report", AsyncMock()
    ) as mock_report, patch(
        "aistock_agent.iterate.scheduler._run_iterate_daily_task", AsyncMock()
    ) as mock_daily:
        await _manual_once(["--once"])

    mock_daily.assert_awaited_once()
    mock_report.assert_not_awaited()


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

    # 日期无关（I1 审查修复）：_run_iterate_build_task 先执行真实日期判断
    # （shanghai_today + chinese_calendar.is_workday），不 patch 时周末/节假日
    # 提前 return，mock_build.assert_awaited() 必挂。参照同文件既有用例模式
    # 显式 patch is_trading_day=True；shanghai_today 无需 patch（True 时继续到 try 块）。
    # Task 5 适配：产片内部逻辑已重构为 produce_cases_daily（单 agent 失败在函数内
    # 隔离），本用例 patch 外层整体性异常（如 import/初始化失败）的兜底路径。
    with patch(
        "aistock_agent.iterate.scheduler.is_trading_day", return_value=True
    ), patch(
        "aistock_agent.iterate.scheduler.produce_cases_daily",
        AsyncMock(side_effect=RuntimeError("node unreachable")),
    ) as mock_build:
        await _run_iterate_build_task()
    mock_build.assert_awaited()
    # 产片失败不抛异常（被内部 try/except 吸收）


@pytest.mark.asyncio
async def test_build_failure_sends_alert_mail(iterate_data_dir: object) -> None:
    """D-3：产片失败触发告警邮件（只告警不中止，D16 语义）。"""
    from aistock_agent.iterate.scheduler import _run_iterate_build_task

    with patch(
        "aistock_agent.iterate.scheduler.is_trading_day", return_value=True
    ), patch(
        "aistock_agent.iterate.scheduler.produce_cases_daily",
        AsyncMock(side_effect=RuntimeError("node unreachable")),
    ), patch(
        "aistock_agent.services.mail_sender.send_mail", return_value=True
    ) as mock_mail:
        await _run_iterate_build_task()
    mock_mail.assert_called_once()
    subject = mock_mail.call_args.args[0]
    assert "迭代产片失败告警" in subject


@pytest.mark.asyncio
async def test_produce_cases_loops_all_agents_and_isolates_failure(monkeypatch) -> None:
    """二期：产片 job 循环 iterable_agent_ids；单 agent 异常不阻断后续。"""
    from aistock_agent.iterate import scheduler
    from aistock_agent.iterate.adapters import iterable_agent_ids

    calls: list[str] = []
    notified: list[object] = []

    async def fake_build(adapter, *, data_dir, force=False):
        calls.append(adapter.agent_id)
        if adapter.agent_id == "review":
            raise RuntimeError("review 产片失败")
        return {"generated": 1, "rejected": 0, "case_ids": ["case_y"], "reasons": []}

    # 告警邮件本体由 test_build_failure_sends_alert_mail 单独覆盖；此处 stub
    # _notify_build_failure 防真实 SMTP，并断言失败 agent 确实走了告警通道。
    monkeypatch.setattr(
        scheduler, "_notify_build_failure", lambda _date, exc: notified.append(exc)
    )
    monkeypatch.setattr(scheduler, "build_cases_for_adapter", fake_build)
    results = await scheduler.produce_cases_daily()
    assert calls == iterable_agent_ids()  # 全部 agent 都被尝试
    assert "review" in results  # 失败 agent 仍记录（失败经告警通道，不抛出）
    assert results["review"] == {"error": "review 产片失败"}
    assert len(notified) == 1  # 仅失败 agent 触发告警
    assert "review 产片失败" in str(notified[0])
