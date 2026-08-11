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
