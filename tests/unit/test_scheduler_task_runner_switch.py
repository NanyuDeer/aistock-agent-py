"""调度灰度开关测试 — 2026-11-18，框架收敛 A3。

锁定三件事：
1. 开关默认关闭 → 走改造前直调路径（不能因为"新代码存在"就切换生产行为）；
2. 白名单空 = 全开；非空 = 只放行列出的 job；
3. `_run_agent` 两条路径返回同一形状（调用方无感）。
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.config import settings
from aistock_agent.services import scheduler as sched
from aistock_agent.services.task_runner import TaskRunResult
from aistock_agent.state.schema import AgentState


def _state() -> AgentState:
    return sched._make_scheduled_state("2026-11-18", intent="morning")


@pytest.fixture()
def enabled(monkeypatch):
    monkeypatch.setattr(settings, "scheduler_task_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "scheduler_task_runner_tasks", "", raising=False)
    yield


def test_switch_off_by_default(monkeypatch) -> None:
    monkeypatch.setattr(settings, "scheduler_task_runner_enabled", False, raising=False)
    assert sched._use_task_runner("morning_briefing") is False


def test_switch_empty_whitelist_means_all(enabled) -> None:
    assert sched._use_task_runner("morning_briefing") is True
    assert sched._use_task_runner("anything_else") is True


def test_switch_whitelist_filters(enabled, monkeypatch) -> None:
    monkeypatch.setattr(
        settings, "scheduler_task_runner_tasks", "rhythm_master_after_close, review_full ", raising=False
    )
    assert sched._use_task_runner("rhythm_master_after_close") is True
    assert sched._use_task_runner("review_full") is True
    assert sched._use_task_runner("morning_briefing") is False


@pytest.mark.asyncio
async def test_legacy_path_uses_patched_module_run(monkeypatch) -> None:
    """关闭时按名取包属性 → 与既有测试 patch(...workers.morning) 的方式对齐。"""
    monkeypatch.setattr(settings, "scheduler_task_runner_enabled", False, raising=False)
    fake = MagicMock()
    fake.run = AsyncMock(return_value={"final_response": "legacy"})

    with patch("aistock_agent.agents.workers.morning", fake, create=True):
        out = await sched._run_agent("morning_briefing", "morning", _state())

    assert out == {"final_response": "legacy"}
    fake.run.assert_awaited_once()


@pytest.mark.asyncio
async def test_enabled_path_goes_through_task_runner(enabled) -> None:
    fake_result = TaskRunResult(
        run_id="r1", task_id="morning_briefing", status="ok", result={"final_response": "recorded"}
    )
    with patch(
        "aistock_agent.services.task_runner.run_agent_task", AsyncMock(return_value=fake_result)
    ) as runner:
        out = await sched._run_agent("morning_briefing", "morning", _state())

    assert out == {"final_response": "recorded"}
    runner.assert_awaited_once()
    assert runner.await_args.args[0] == "morning_briefing"


@pytest.mark.asyncio
async def test_enabled_path_returns_empty_dict_when_result_none(enabled) -> None:
    """task_runner 的 result 允许为 None（数据类/空产物）→ 调用方拿 {}，不拿 None。"""
    fake_result = TaskRunResult(run_id="r1", task_id="t", status="degraded", result=None)
    with patch(
        "aistock_agent.services.task_runner.run_agent_task", AsyncMock(return_value=fake_result)
    ):
        out = await sched._run_agent("t", "morning", _state())
    assert out == {}
