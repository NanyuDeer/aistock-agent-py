"""iterate 调度 —— 注册 job 与手动触发"""

from unittest.mock import AsyncMock, patch

import pytest


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
    from aistock_agent.iterate.case_builder import mark_iterated
    from aistock_agent.iterate.scheduler import _run_iterate_daily_task

    case_id = "case_20260731_us_market_surge"
    mark_iterated(case_id)
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
