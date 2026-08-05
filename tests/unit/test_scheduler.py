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

import json
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
        shutdown_scheduler,
        start_scheduler,
    )

    start_scheduler()
    scheduler = get_scheduler()
    jobs = scheduler.get_jobs()
    job_ids = [j.id for j in jobs]
    assert "morning_briefing" in job_ids
    assert "broadcast_chain" in job_ids
    assert "evening_chain" in job_ids
    assert "review_report" not in job_ids
    assert "snapshot_build" not in job_ids
    assert "iterate_analysis" not in job_ids
    assert {str(job.trigger.timezone) for job in jobs} == {"Asia/Shanghai"}
    shutdown_scheduler()


def test_start_scheduler_explicitly_passes_configured_timezone_to_cron() -> None:
    """CronTrigger 必须显式绑定调度配置时区，不能依赖进程本地时区。"""
    from aistock_agent.services import scheduler

    with patch.object(
        scheduler.CronTrigger,
        "from_crontab",
        wraps=scheduler.CronTrigger.from_crontab,
    ) as from_crontab:
        scheduler.start_scheduler()

    assert from_crontab.call_count == 3
    assert all(
        call.kwargs["timezone"] == scheduler.settings.scheduler_timezone
        for call in from_crontab.call_args_list
    )


def test_qa_mode_hard_disables_scheduler_even_if_scheduler_enabled(monkeypatch) -> None:
    """QA_MODE=true 不得依赖另一个环境变量才能避免真实上游任务。"""
    from aistock_agent.services import scheduler

    monkeypatch.setattr(scheduler.settings, "qa_mode", "true")
    monkeypatch.setattr(scheduler.settings, "scheduler_enabled", True)

    scheduler.start_scheduler()

    assert scheduler.get_scheduler().get_jobs() == []
    assert scheduler.get_scheduler().running is False


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
async def test_morning_task_passes_the_same_shanghai_date_to_session_and_report():
    """交易日判断和晨报状态必须使用同一个上海自然日。"""
    from datetime import date

    from aistock_agent.services.scheduler import _run_morning_task

    shanghai_date = date(2026, 7, 24)
    with (
        patch("aistock_agent.services.scheduler.shanghai_today", return_value=shanghai_date),
        patch("aistock_agent.services.scheduler.is_trading_day", return_value=True),
        patch("aistock_agent.agents.workers.morning", create=True) as mock_agent,
    ):
        mock_agent.run = AsyncMock(return_value={"final_response": "晨报内容"})
        await _run_morning_task()

    state = mock_agent.run.await_args.args[0]
    assert state["session_id"] == "scheduled_morning_2026-07-24"
    assert state["report_date"] == "2026-07-24"


@pytest.mark.asyncio
async def test_scheduler_review_task_calls_review_agent():
    """_run_review_task 调用 review.run()"""
    from aistock_agent.agents.workers import review as review_module
    from aistock_agent.services.scheduler import _run_review_task

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
    from aistock_agent.agents.workers import iterate as iterate_module
    from aistock_agent.services.scheduler import _run_iterate_task

    with patch.object(iterate_module, "run", new_callable=AsyncMock) as mock_run:
        mock_run.return_value = {"final_response": '{"status": "normal"}'}
        await _run_iterate_task()
    mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_evening_chain_persists_each_artifact_before_next_step() -> None:
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        side_effect=[
            {
                "id": 1,
                "report_type": "review",
                "status": "completed",
                "data_source": "review_agent",
                "created_at": "2026-07-24T15:30:00+08:00",
                "content": {"text": "saved"},
            },
            {
                "id": 2,
                "report_type": "market_snapshot",
                "status": "completed",
                "data_source": "snapshot_builder",
                "created_at": "2026-07-24T15:35:00+08:00",
                "content": {"text": "snapshot"},
            },
            {
                "id": 3,
                "report_type": "iterate",
                "status": "completed",
                "data_source": "iterate_analyzer",
                "created_at": "2026-07-24T15:40:00+08:00",
                "content": {"text": '{"status": "normal"}'},
            },
        ]
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})
    build_brief = AsyncMock(return_value=True)

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={"date": "2026-07-24", "summary": "snapshot"},
        ) as snapshot,
        patch.object(
            iterate,
            "run",
            new=AsyncMock(return_value={"final_response": '{"status": "normal"}'}),
        ),
        patch.object(scheduler, "build_and_persist_brief", build_brief),
        patch.object(
            broadcast,
            "run",
            new=AsyncMock(return_value={"final_response": "broadcast"}),
        ) as run_broadcast,
    ):
        await scheduler._run_evening_chain_task()

    snapshot.assert_called_once_with("2026-07-24")
    assert api.save_analysis_report.await_args_list[0].kwargs["report_type"] == "market_snapshot"
    assert api.save_analysis_report.await_args_list[1].kwargs["report_type"] == "iterate"
    assert api.save_analysis_report.await_args_list[0].kwargs["data_source"] == "snapshot_builder"
    assert api.save_analysis_report.await_args_list[1].kwargs["data_source"] == "iterate_analyzer"
    assert [call.args[0] for call in api.get_analysis_report.await_args_list] == [
        "review",
        "market_snapshot",
        "iterate",
    ]
    build_brief.assert_awaited_once_with("evening", "2026-07-24")
    state = run_broadcast.await_args.args[0]
    assert state["brief_type"] == "evening"
    assert state["report_date"] == "2026-07-24"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "review_report",
    [
        None,
        {
            "id": 1,
            "report_type": "review",
            "status": "completed",
            "data_source": "review_agent",
            "created_at": "2026-07-24T15:30:00+08:00",
            "content": {},
        },
    ],
)
async def test_evening_chain_stops_when_review_is_missing_or_incomplete(
    review_report: object,
) -> None:
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(return_value=review_report)
    build_brief = AsyncMock(return_value=True)

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch("aistock_agent.services.snapshot_builder.build_snapshot") as snapshot,
        patch.object(scheduler, "build_and_persist_brief", build_brief),
        patch.object(
            broadcast,
            "run",
            new=AsyncMock(return_value={"final_response": "broadcast"}),
        ) as run_broadcast,
    ):
        await scheduler._run_evening_chain_task()

    snapshot.assert_not_called()
    build_brief.assert_not_awaited()
    run_broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_evening_chain_skips_snapshot_when_review_artifact_is_not_traceable() -> None:
    """失败的复盘记录不是可驱动快照的已完成工件。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        return_value={
            "id": 42,
            "report_type": "review",
            "status": "failed",
            "data_source": "review_agent",
            "created_at": "2026-07-24T15:30:00+08:00",
            "content": {"text": "review"},
        }
    )
    build_brief = AsyncMock(return_value=True)

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch("aistock_agent.services.snapshot_builder.build_snapshot") as snapshot,
        patch.object(iterate, "run", new=AsyncMock()) as run_iterate,
        patch.object(scheduler, "build_and_persist_brief", build_brief),
        patch.object(broadcast, "run", new=AsyncMock()) as run_broadcast,
    ):
        await scheduler._run_evening_chain_task()

    snapshot.assert_not_called()
    run_iterate.assert_not_awaited()
    build_brief.assert_not_awaited()
    run_broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_morning_broadcast_chain_persists_brief_before_broadcast() -> None:
    from datetime import date

    from aistock_agent.agents.workers import broadcast, hot_burst, morning, wind_leader
    from aistock_agent.services import scheduler

    build_brief = AsyncMock(return_value=True)
    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(morning, "run", new=AsyncMock(return_value={"final_response": "morning"})),
        patch.object(wind_leader, "run", new=AsyncMock(return_value={"final_response": "wind"})),
        patch.object(hot_burst, "run", new=AsyncMock(return_value={"final_response": "burst"})),
        patch.object(scheduler, "build_and_persist_brief", build_brief),
        patch.object(
            broadcast,
            "run",
            new=AsyncMock(return_value={"final_response": "broadcast"}),
        ) as run_broadcast,
    ):
        await scheduler._run_broadcast_task()

    build_brief.assert_awaited_once_with("morning", "2026-07-24")
    state = run_broadcast.await_args.args[0]
    assert state["brief_type"] == "morning"
    assert state["report_date"] == "2026-07-24"


@pytest.mark.asyncio
@pytest.mark.parametrize("iterate_status", ("error", "skip"))
async def test_evening_chain_does_not_persist_invalid_iterate_artifact(
    iterate_status: str,
) -> None:
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        side_effect=[
            {
                "id": 1,
                "report_type": "review",
                "status": "completed",
                "data_source": "review_agent",
                "created_at": "2026-07-24T15:30:00+08:00",
                "content": {"text": "review"},
            },
            {
                "id": 2,
                "report_type": "market_snapshot",
                "status": "completed",
                "data_source": "snapshot_builder",
                "created_at": "2026-07-24T15:35:00+08:00",
                "content": {"text": "snapshot"},
            },
        ]
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={"date": "2026-07-24"},
        ),
        patch.object(
            iterate,
            "run",
            new=AsyncMock(return_value={"final_response": f'{{"status": "{iterate_status}"}}'}),
        ),
        patch.object(
            scheduler, "build_and_persist_brief", new=AsyncMock(return_value=True)
        ) as build_brief,
        patch.object(broadcast, "run", new=AsyncMock()) as run_broadcast,
    ):
        await scheduler._run_evening_chain_task()

    persisted_types = [
        call.kwargs["report_type"] for call in api.save_analysis_report.await_args_list
    ]
    assert persisted_types == ["market_snapshot"]
    build_brief.assert_not_awaited()
    run_broadcast.assert_not_awaited()


@pytest.mark.asyncio
async def test_evening_chain_skips_error_snapshot_and_iterate() -> None:
    """缺少上游事实的快照不能持久化为有效工件，也不能驱动迭代。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import iterate, review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        return_value={
            "id": 1,
            "report_type": "review",
            "status": "completed",
            "data_source": "review_agent",
            "created_at": "2026-07-24T15:30:00+08:00",
            "content": {"text": "review"},
        }
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})
    build_brief = AsyncMock(return_value=True)

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={"error": "missing_reports"},
        ),
        patch.object(iterate, "run", new=AsyncMock()) as run_iterate,
        patch.object(scheduler, "build_and_persist_brief", build_brief),
    ):
        await scheduler._run_evening_chain_task()

    api.save_analysis_report.assert_not_awaited()
    run_iterate.assert_not_awaited()
    build_brief.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, {"id": 2}])
async def test_evening_chain_stops_when_snapshot_is_not_traceable(
    saved: object,
) -> None:
    """快照保存失败或回读后不可追溯时，不生成晚报。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    review_report = {
        "id": 1,
        "report_type": "review",
        "status": "completed",
        "data_source": "review_agent",
        "created_at": "2026-07-24T15:30:00+08:00",
        "content": {"text": "review"},
    }
    api = MagicMock()
    api.get_analysis_report = AsyncMock(side_effect=[review_report, None])
    api.save_analysis_report = AsyncMock(return_value=saved)
    build_brief = AsyncMock(return_value=True)

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={"date": "2026-07-24", "summary": "snapshot"},
        ),
        patch.object(iterate, "run", new=AsyncMock()) as run_iterate,
        patch.object(scheduler, "build_and_persist_brief", build_brief),
        patch.object(broadcast, "run", new=AsyncMock()) as run_broadcast,
    ):
        await scheduler._run_evening_chain_task()

    run_iterate.assert_not_awaited()
    build_brief.assert_not_awaited()
    run_broadcast.assert_not_awaited()
    expected_reads = 1 if saved is None else 2
    assert api.get_analysis_report.await_count == expected_reads


@pytest.mark.asyncio
@pytest.mark.parametrize("saved", [None, {"id": 3}])
async def test_evening_chain_stops_when_iterate_is_not_traceable(
    saved: object,
) -> None:
    """迭代保存失败或回读后不可追溯时，不生成晚报。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        side_effect=[
            {
                "id": 1,
                "report_type": "review",
                "status": "completed",
                "data_source": "review_agent",
                "created_at": "2026-07-24T15:30:00+08:00",
                "content": {"text": "review"},
            },
            {
                "id": 2,
                "report_type": "market_snapshot",
                "status": "completed",
                "data_source": "snapshot_builder",
                "created_at": "2026-07-24T15:35:00+08:00",
                "content": {"text": "snapshot"},
            },
            None,
        ]
    )
    api.save_analysis_report = AsyncMock(side_effect=[{"id": 2}, saved])
    build_brief = AsyncMock(return_value=True)

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={"date": "2026-07-24", "summary": "snapshot"},
        ),
        patch.object(
            iterate,
            "run",
            new=AsyncMock(return_value={"final_response": '{"status": "normal"}'}),
        ),
        patch.object(scheduler, "build_and_persist_brief", build_brief),
        patch.object(broadcast, "run", new=AsyncMock()) as run_broadcast,
    ):
        await scheduler._run_evening_chain_task()

    build_brief.assert_not_awaited()
    run_broadcast.assert_not_awaited()
    expected_reads = 2 if saved is None else 3
    assert api.get_analysis_report.await_count == expected_reads


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("id", 0),
        ("report_type", "wrong"),
        ("status", "failed"),
        ("data_source", ""),
        ("created_at", ""),
        ("content", {}),
    ],
)
def test_traceable_report_validation_rejects_invalid_required_fields(
    field: str,
    invalid_value: object,
) -> None:
    from aistock_agent.services.scheduler import _is_traceable_completed_report

    report: dict[str, object] = {
        "id": 1,
        "report_type": "iterate",
        "status": "completed",
        "data_source": "iterate_analyzer",
        "created_at": "2026-07-24T15:40:00+08:00",
        "content": {"text": "iterate"},
    }
    report[field] = invalid_value

    assert _is_traceable_completed_report(report, "iterate") is False


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


# ── schema 2.0 持久化契约：market_snapshot / iterate 不得写入原始 JSON ──


def _traceable_report(
    report_type: str, report_id: int, content: dict[str, object]
) -> dict[str, object]:
    """构造可追溯的已完成持久化行。"""
    return {
        "id": report_id,
        "report_type": report_type,
        "status": "completed",
        "data_source": f"{report_type}_source",
        "created_at": "2026-07-24T15:30:00+08:00",
        "content": content,
    }


@pytest.mark.asyncio
async def test_evening_chain_persists_market_snapshot_with_controlled_brief_summary() -> None:
    """market_snapshot 只能持久化由允许指标构造的 brief_summary.v1。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    snapshot_payload = {
        "date": "2026-07-24",
        "dimension_1_coverage": {
            "hit_rate": 0.72,
            "new_coverage_rate": 0.15,
            "overlap_hits": [],
            "missing_in_morning": [],
            "over_focused": [],
        },
    }
    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        side_effect=[
            _traceable_report("review", 1, {"text": "review"}),
            _traceable_report("market_snapshot", 2, {"text": "should-not-be-used"}),
            _traceable_report("iterate", 3, {"text": '{"status": "normal"}'}),
        ]
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value=snapshot_payload,
        ),
        patch.object(
            iterate,
            "run",
            new=AsyncMock(
                return_value={"final_response": '{"status": "normal", "summary": "今日无显著异常"}'}
            ),
        ),
        patch.object(scheduler, "build_and_persist_brief", new=AsyncMock(return_value=True)),
        patch.object(broadcast, "run", new=AsyncMock()),
    ):
        await scheduler._run_evening_chain_task()

    snapshot_call = api.save_analysis_report.await_args_list[0]
    assert snapshot_call.kwargs["report_type"] == "market_snapshot"
    content = snapshot_call.kwargs["content"]
    summary = content["brief_summary"]
    assert summary["schema_version"] == "brief_summary.v1"
    assert summary["summary"] == "市场快照（2026-07-24）：板块命中率 0.72，新覆盖率 0.15"
    assert content["snapshot"] == snapshot_payload
    assert "text" not in content


@pytest.mark.asyncio
async def test_evening_chain_persists_iterate_with_controlled_brief_summary() -> None:
    """iterate 只能持久化由状态和合法维度构造的 brief_summary.v1。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    iterate_summary = "维度二方向-强度偏差触发，建议复核晨报板块打分"
    iterate_payload = {
        "date": "2026-07-24",
        "status": "alert",
        "summary": iterate_summary,
        "triggered_dimensions": ["dimension_2"],
    }
    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        side_effect=[
            _traceable_report("review", 1, {"text": "review"}),
            _traceable_report("market_snapshot", 2, {"text": "snapshot"}),
            _traceable_report("iterate", 3, {"text": "should-not-be-used"}),
        ]
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={
                "date": "2026-07-24",
                "dimension_1_coverage": {"hit_rate": 0.5, "new_coverage_rate": 0.1},
            },
        ),
        patch.object(
            iterate,
            "run",
            new=AsyncMock(
                return_value={"final_response": json.dumps(iterate_payload, ensure_ascii=False)}
            ),
        ),
        patch.object(scheduler, "build_and_persist_brief", new=AsyncMock(return_value=True)),
        patch.object(broadcast, "run", new=AsyncMock()),
    ):
        await scheduler._run_evening_chain_task()

    iterate_call = api.save_analysis_report.await_args_list[1]
    assert iterate_call.kwargs["report_type"] == "iterate"
    content = iterate_call.kwargs["content"]
    assert content["brief_summary"]["schema_version"] == "brief_summary.v1"
    assert content["brief_summary"]["summary"] == "检测到异常维度：dimension_2"
    assert content["iterate_payload"] == iterate_payload
    assert "text" not in content


@pytest.mark.asyncio
async def test_evening_chain_ignores_missing_llm_summary_for_legal_iterate_alert() -> None:
    """合法 alert 不依赖 LLM summary，仍由受控维度产生 Brief 摘要。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    iterate_payload = {
        "date": "2026-07-24",
        "status": "alert",
        "triggered_dimensions": ["dimension_2"],
    }
    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        side_effect=[
            _traceable_report("review", 1, {"text": "review"}),
            _traceable_report("market_snapshot", 2, {"text": "snapshot"}),
            _traceable_report("iterate", 3, {"text": "should-not-be-used"}),
        ]
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={
                "date": "2026-07-24",
                "dimension_1_coverage": {"hit_rate": 0.5, "new_coverage_rate": 0.1},
            },
        ),
        patch.object(
            iterate,
            "run",
            new=AsyncMock(
                return_value={"final_response": json.dumps(iterate_payload, ensure_ascii=False)}
            ),
        ),
        patch.object(scheduler, "build_and_persist_brief", new=AsyncMock(return_value=True)),
        patch.object(broadcast, "run", new=AsyncMock()),
    ):
        await scheduler._run_evening_chain_task()

    iterate_call = api.save_analysis_report.await_args_list[1]
    content = iterate_call.kwargs["content"]
    assert content["brief_summary"]["summary"] == "检测到异常维度：dimension_2"
    assert "text" not in content


def test_extract_snapshot_summary_returns_empty_for_invalid_payload() -> None:
    """缺少 dimension_1_coverage 或指标类型不对时返回空字符串，触发 Brief 降级。"""
    from aistock_agent.services.scheduler import _extract_snapshot_summary

    assert _extract_snapshot_summary(None) == ""
    assert _extract_snapshot_summary({"date": "2026-07-24"}) == ""
    assert _extract_snapshot_summary({
        "date": "2026-07-24",
        "dimension_1_coverage": {},
    }) == ""
    assert _extract_snapshot_summary({
        "date": "2026-07-24",
        "dimension_1_coverage": {"hit_rate": "not-a-number", "new_coverage_rate": 0.1},
    }) == ""
    # bool 不是合法指标（Python 中 bool 是 int 子类，需显式排除）
    assert _extract_snapshot_summary({
        "date": "2026-07-24",
        "dimension_1_coverage": {"hit_rate": True, "new_coverage_rate": 0.1},
    }) == ""


def test_extract_iterate_summary_ignores_llm_summary_and_rejects_invalid_state() -> None:
    """iterate normal 固定摘要，未知状态或非法维度不得构成 Brief 事实。"""
    from aistock_agent.services.scheduler import _extract_iterate_summary

    assert _extract_iterate_summary(None) == ""
    assert _extract_iterate_summary({"status": "alert"}) == ""
    assert _extract_iterate_summary({"status": "normal", "summary": ""}) == ""
    assert _extract_iterate_summary(
        {"status": "normal", "summary": "LLM 改写", "triggered_dimensions": []}
    ) == "今日无显著异常"
    assert _extract_iterate_summary({"status": "skip", "summary": "今日无显著异常"}) == ""
    assert _extract_iterate_summary({"status": "alert", "triggered_dimensions": ["unknown"]}) == ""


# ── 事件驱动 evening_chain 重构测试 ──


@pytest.mark.asyncio
async def test_publish_review_quick_event_publishes_to_event_bus():
    """_publish_review_quick_event 成功发布事件。"""
    from unittest.mock import AsyncMock, MagicMock, patch
    from aistock_agent.services.scheduler import _publish_review_quick_event

    mock_bus = AsyncMock()
    mock_bus.publish = AsyncMock(return_value="evt-1")

    with patch("aistock_agent.services.scheduler._get_event_bus", new_callable=AsyncMock, return_value=mock_bus):
        with patch("aistock_agent.services.scheduler.is_trading_day", return_value=True):
            with patch("aistock_agent.services.scheduler.shanghai_today") as mock_today:
                from datetime import date
                mock_today.return_value = date(2026, 7, 30)
                await _publish_review_quick_event()

    mock_bus.publish.assert_called_once()
    call_args = mock_bus.publish.call_args
    assert call_args[0][0] == "review_quick"
    assert call_args[1]["payload"]["report_date"] == "2026-07-30"


@pytest.mark.asyncio
async def test_publish_review_quick_event_skips_non_trading_day():
    """非交易日跳过。"""
    from unittest.mock import patch
    from aistock_agent.services.scheduler import _publish_review_quick_event

    with patch("aistock_agent.services.scheduler.is_trading_day", return_value=False):
        await _publish_review_quick_event()  # 不应抛异常


def test_start_scheduler_registers_quick_full_crons_when_enabled():
    """quick_snapshot_enabled=True 时注册 review_quick/review_full cron。"""
    from unittest.mock import MagicMock, patch
    from aistock_agent.services.scheduler import start_scheduler

    mock_scheduler = MagicMock()
    with patch("aistock_agent.services.scheduler.get_scheduler", return_value=mock_scheduler):
        with patch("aistock_agent.services.scheduler.settings") as mock_settings:
            mock_settings.qa_mode_enabled = False
            mock_settings.scheduler_enabled = True
            mock_settings.quick_snapshot_enabled = True
            mock_settings.scheduler_morning_cron = "50 8 * * 1-5"
            mock_settings.scheduler_broadcast_cron = "0 9 * * 1-5"
            mock_settings.scheduler_review_quick_cron = "30 15 * * 1-5"
            mock_settings.scheduler_review_full_cron = "30 20 * * 1-5"
            mock_settings.scheduler_timezone = "Asia/Shanghai"
            start_scheduler()

    job_ids = [call.kwargs["id"] for call in mock_scheduler.add_job.call_args_list]
    assert "review_quick" in job_ids
    assert "review_full" in job_ids
    assert "evening_chain" not in job_ids


def test_start_scheduler_registers_legacy_evening_chain_when_disabled():
    """quick_snapshot_enabled=False 时保留旧 evening_chain。"""
    from unittest.mock import MagicMock, patch
    from aistock_agent.services.scheduler import start_scheduler

    mock_scheduler = MagicMock()
    with patch("aistock_agent.services.scheduler.get_scheduler", return_value=mock_scheduler):
        with patch("aistock_agent.services.scheduler.settings") as mock_settings:
            mock_settings.qa_mode_enabled = False
            mock_settings.scheduler_enabled = True
            mock_settings.quick_snapshot_enabled = False
            mock_settings.scheduler_morning_cron = "50 8 * * 1-5"
            mock_settings.scheduler_broadcast_cron = "0 9 * * 1-5"
            mock_settings.scheduler_review_cron = "30 15 * * 1-5"
            mock_settings.scheduler_timezone = "Asia/Shanghai"
            start_scheduler()

    job_ids = [call.kwargs["id"] for call in mock_scheduler.add_job.call_args_list]
    assert "evening_chain" in job_ids
    assert "review_quick" not in job_ids


# ── 手动补跑晚间链路（/admin/trigger/evening_chain 支持） ──


@pytest.mark.asyncio
async def test_evening_chain_explicit_report_date_skips_trading_day_check() -> None:
    """显式传 report_date 时跳过交易日检查（非交易日也执行完整链路）。"""
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import broadcast, iterate, review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(
        side_effect=[
            _traceable_report("review", 1, {"text": "review"}),
            _traceable_report("market_snapshot", 2, {"text": "snapshot"}),
            _traceable_report("iterate", 3, {"text": '{"status": "normal"}'}),
        ]
    )
    api.save_analysis_report = AsyncMock(return_value={"id": 1})
    build_brief = AsyncMock(return_value=True)

    with (
        # 非交易日：只有显式传日期的手动补跑才允许执行
        patch.object(scheduler, "is_trading_day", return_value=False),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
        patch(
            "aistock_agent.services.snapshot_builder.build_snapshot",
            return_value={"date": "2026-07-24", "summary": "snapshot"},
        ),
        patch.object(
            iterate,
            "run",
            new=AsyncMock(return_value={"final_response": '{"status": "normal"}'}),
        ),
        patch.object(scheduler, "build_and_persist_brief", build_brief),
        patch.object(broadcast, "run", new=AsyncMock()),
    ):
        result = await scheduler._run_evening_chain_task(report_date="2026-07-24")

    assert result["status"] == "ok"
    assert result["report_date"] == "2026-07-24"
    assert result["stages"]["broadcast"] == "ok"
    build_brief.assert_awaited_once_with("evening", "2026-07-24")


@pytest.mark.asyncio
async def test_evening_chain_without_date_skips_non_trading_day() -> None:
    """缺省日期且非交易日时返回 skipped（原调度行为保持）。"""
    from datetime import date

    from aistock_agent.services import scheduler

    with (
        patch.object(scheduler, "is_trading_day", return_value=False),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 26)),
    ):
        result = await scheduler._run_evening_chain_task()

    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_evening_chain_returns_failed_stage_when_review_invalid() -> None:
    """review 产物不可追溯时返回 failed + stage=review。"""
    from datetime import date
    from unittest.mock import MagicMock

    from aistock_agent.agents.workers import review
    from aistock_agent.services import scheduler

    api = MagicMock()
    api.get_analysis_report = AsyncMock(return_value=None)

    with (
        patch.object(scheduler, "is_trading_day", return_value=True),
        patch.object(scheduler, "shanghai_today", return_value=date(2026, 7, 24)),
        patch.object(scheduler, "node_api", api),
        patch.object(review, "run", new=AsyncMock(return_value={"final_response": "review"})),
    ):
        result = await scheduler._run_evening_chain_task()

    assert result["status"] == "failed"
    assert result["stage"] == "review"
