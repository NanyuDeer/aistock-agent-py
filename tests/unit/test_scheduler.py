"""定时调度服务测试 — APScheduler 集成

验证：
- get_scheduler 返回单例
- start_scheduler 注册 4 个定时任务（morning/review/snapshot/iterate）
- _run_morning_task 非交易日跳过、交易日执行
- _run_review_task 调用 review.run()
- _run_snapshot_task 调用 build_snapshot()
- _run_iterate_task 调用 iterate.run()

mock 路径说明：
- morning_agent / review_agent / iterate_agent 均在对应 task 函数内部
  import（from aistock_agent.agents.workers import <module>），因此通过
  patch.object(<module>, "run", ...) 替换模块的 run 属性。
- build_snapshot 在 _run_snapshot_task 函数内部 import
  （from aistock_agent.services.snapshot_builder import build_snapshot），
  patch import 源 aistock_agent.services.snapshot_builder.build_snapshot 即生效。
- is_trading_day 在 scheduler 模块顶部 import，patch
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


@pytest.mark.asyncio
async def test_scheduler_review_task_calls_review_agent():
    """_run_review_task 调用 review.run()"""
    from aistock_agent.services.scheduler import _run_review_task
    from aistock_agent.agents.workers import review as review_module

    with patch.object(review_module, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"final_response": "复盘报告"}
        with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
            await _run_review_task()
    mock_run.assert_called_once()


@pytest.mark.asyncio
@patch("aistock_agent.services.snapshot_builder.build_snapshot")
@patch("aistock_agent.services.scheduler.is_trading_day", return_value=True)
async def test_scheduler_snapshot_task_calls_build_snapshot(mock_trading, mock_build):
    """_run_snapshot_task 调用 build_snapshot()"""
    from aistock_agent.services.scheduler import _run_snapshot_task

    mock_build.return_value = {"date": "2026-07-08", "error": None}
    await _run_snapshot_task()
    mock_build.assert_called_once()


@pytest.mark.asyncio
@patch("aistock_agent.services.scheduler.is_trading_day", return_value=True)
async def test_scheduler_iterate_task_calls_iterate_agent(mock_trading):
    """_run_iterate_task 调用 iterate.run()"""
    from aistock_agent.services.scheduler import _run_iterate_task
    from aistock_agent.agents.workers import iterate as iterate_module

    with patch.object(iterate_module, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"final_response": '{"status": "normal"}'}
        await _run_iterate_task()
    mock_run.assert_called_once()


# ── 事件传导：major_events → event agent ──


@pytest.mark.asyncio
async def test_morning_task_triggers_event_conduction_for_major_events():
    """scheduler morning → major_events → event agent 传导"""
    import asyncio

    from aistock_agent.services.event_conduction import EventConductionResult
    from aistock_agent.services.scheduler import _run_morning_task

    major_events = [
        {"title": "美联储加息", "summary": "加息25bp"},
        {"title": "通胀数据公布", "summary": "CPI 3.2%"},
    ]
    morning_result = {
        "final_response": '{"display_report": {}}',
        "analysis_reports": {"major_events": major_events},
    }

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
        with patch("aistock_agent.agents.workers.morning", create=True) as mock_agent:
            mock_agent.run = AsyncMock(return_value=morning_result)
            with patch(
                "aistock_agent.services.event_conduction.run_single_event_conduction",
                new_callable=AsyncMock,
            ) as mock_event:
                mock_event.return_value = EventConductionResult(
                    success=True,
                    event_id="evt_test",
                    title="test",
                    event_generated=True,
                    persisted=True,
                )
                await _run_morning_task()
                # fire-and-forget tasks 需要等事件循环处理
                await asyncio.sleep(0.1)

    # 验证每个 major_event 都触发了事件传导
    assert mock_event.call_count == 2
    called_events = [call.args[0] for call in mock_event.call_args_list]
    called_titles = [e["title"] for e in called_events]
    assert "美联储加息" in called_titles
    assert "通胀数据公布" in called_titles


@pytest.mark.asyncio
async def test_morning_task_no_major_events_no_conduction():
    """无 major_events 时不触发事件传导"""
    import asyncio

    from aistock_agent.services.scheduler import _run_morning_task

    morning_result = {
        "final_response": '{"display_report": {}}',
        "analysis_reports": {"major_events": []},
    }

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
        with patch("aistock_agent.agents.workers.morning", create=True) as mock_agent:
            mock_agent.run = AsyncMock(return_value=morning_result)
            with patch(
                "aistock_agent.services.event_conduction.run_single_event_conduction",
                new_callable=AsyncMock,
            ) as mock_event:
                await _run_morning_task()
                await asyncio.sleep(0.1)

    mock_event.assert_not_called()


@pytest.mark.asyncio
async def test_morning_task_single_event_failure_does_not_block():
    """单个事件失败不阻断其他事件"""
    import asyncio

    from aistock_agent.services.event_conduction import EventConductionResult
    from aistock_agent.services.scheduler import _run_morning_task

    major_events = [
        {"title": "正常事件"},
        {"title": "崩溃事件"},
        {"title": "另一个正常事件"},
    ]
    morning_result = {
        "final_response": '{"display_report": {}}',
        "analysis_reports": {"major_events": major_events},
    }

    call_count = [0]

    async def mock_conduction(event):
        call_count[0] += 1
        if event["title"] == "崩溃事件":
            raise RuntimeError("模拟崩溃")
        return EventConductionResult(
            success=True,
            event_id=f"evt_{call_count[0]}",
            title=event["title"],
            event_generated=True,
            persisted=True,
        )

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
        with patch("aistock_agent.agents.workers.morning", create=True) as mock_agent:
            mock_agent.run = AsyncMock(return_value=morning_result)
            with patch(
                "aistock_agent.services.event_conduction.run_single_event_conduction",
                side_effect=mock_conduction,
            ):
                await _run_morning_task()
                await asyncio.sleep(0.2)

    # 3 个事件都被调用了（崩溃的不影响其他）
    assert call_count[0] == 3


@pytest.mark.asyncio
async def test_scheduler_review_task_passes_persistence_context():
    from datetime import date

    from aistock_agent.agents.workers import review as review_module
    from aistock_agent.services.scheduler import _run_review_task

    with patch.object(review_module, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"final_response": "复盘报告"}
        with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
            await _run_review_task()

    state = mock_run.await_args.args[0]
    assert state["trigger_source"] == "scheduler"
    assert state["report_date"] == date.today().isoformat()
