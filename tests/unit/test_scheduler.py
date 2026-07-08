"""定时调度服务测试 — APScheduler 集成

验证：
- get_scheduler 返回单例
- start_scheduler 注册 4 个定时任务（morning/review/snapshot/iterate）
- _run_morning_task 非交易日跳过、交易日执行

mock 路径说明：morning_agent 在 _run_morning_task 函数内部 import
（from aistock_agent.agents.workers import morning as morning_agent），
因此 patch import 源 aistock_agent.agents.workers.morning，而非 scheduler
模块上的属性。is_trading_day 在 scheduler 模块顶部 import，patch
aistock_agent.services.scheduler.is_trading_day。
"""

from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_scheduler_singleton():
    """每个测试后清理 scheduler 单例，避免跨测试状态泄漏。"""
    yield
    try:
        from aistock_agent.services.scheduler import shutdown_scheduler

        shutdown_scheduler()
    except Exception:
        pass


def test_get_scheduler_returns_singleton():
    """get_scheduler 返回同一实例（单例）"""
    from aistock_agent.services.scheduler import get_scheduler, shutdown_scheduler

    s1 = get_scheduler()
    s2 = get_scheduler()
    assert s1 is s2
    shutdown_scheduler()


@pytest.mark.asyncio
async def test_start_scheduler_initializes_jobs():
    """start_scheduler 注册了 4 个定时任务"""
    from aistock_agent.services.scheduler import (
        get_scheduler,
        start_scheduler,
        shutdown_scheduler,
    )

    start_scheduler()
    scheduler = get_scheduler()
    jobs = scheduler.get_jobs()
    job_ids = [j.id for j in jobs]
    assert "morning_briefing" in job_ids
    assert "review_report" in job_ids
    assert "snapshot_build" in job_ids
    assert "iterate_analysis" in job_ids
    shutdown_scheduler()


@pytest.mark.asyncio
async def test_morning_task_skips_non_trading_day():
    """非交易日跳过晨报生成"""
    from aistock_agent.services.scheduler import _run_morning_task

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=False):
        # morning_agent 在函数内 import，patch import 源模块
        with patch("aistock_agent.agents.workers.morning", create=True) as mock_agent:
            await _run_morning_task()
            mock_agent.run.assert_not_called()


@pytest.mark.asyncio
async def test_morning_task_runs_on_trading_day():
    """交易日正常执行晨报生成"""
    from aistock_agent.services.scheduler import _run_morning_task

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
        # morning_agent 在函数内 import，patch import 源模块
        with patch("aistock_agent.agents.workers.morning", create=True) as mock_agent:
            mock_agent.run = AsyncMock(return_value={"final_response": "晨报内容"})
            await _run_morning_task()
            mock_agent.run.assert_called_once()
