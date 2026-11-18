"""任务执行外壳（task_runner）测试 — 2026-11-18，框架收敛 A2。

锁定五件事：
1. 成功 → 记 ok，产物键落库；
2. 抛异常 → 记 failed（含异常文本）**并原样抛出**（不能把失败伪装成成功）；
3. 空产物 → degraded（跑完了但什么都没产）；
4. **AgentState 未声明的返回键必须原样保留** —— 这是否决"套单节点图"方案的原因：
   LangGraph 会静默丢弃未声明键（cached / audio_path / event_id …），下游读到空值；
5. 数据类任务同样有执行记录，且不套图。
"""
from __future__ import annotations

from typing import Any

import pytest

from aistock_agent.agents.registry import AgentSpec
from aistock_agent.config import settings
from aistock_agent.services import task_runner, task_store
from aistock_agent.state.schema import AgentState


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "task_runs_path", str(tmp_path / "task_runs.db"), raising=False)
    task_runner.reset_store_ready_for_tests()
    yield
    task_runner.reset_store_ready_for_tests()


def _fake_spec(run, *, retryable: bool = False) -> AgentSpec:
    return AgentSpec("fake", "fake_agent", run, schedulable=True, retryable=retryable)


def _state() -> AgentState:
    return task_runner.make_agent_state(intent="fake", session_id="s1", report_date="2026-11-18")


@pytest.mark.asyncio
async def test_agent_task_ok_records_result_keys(env, monkeypatch) -> None:
    async def good(state: AgentState) -> dict[str, object]:
        assert state["trigger_source"] == "scheduler"
        return {"final_response": "晨报正文", "cards": []}

    monkeypatch.setattr(task_runner, "get_agent", lambda name: _fake_spec(good))

    result = await task_runner.run_agent_task("t_ok", "fake", _state())

    assert result.status == "ok"
    assert result.result is not None
    row = await task_store.get_run(result.run_id)
    assert row is not None
    assert row["status"] == "ok"
    assert row["agent"] == "fake"
    assert set(row["result_keys"]) == {"final_response", "cards"}


@pytest.mark.asyncio
async def test_undeclared_state_keys_are_preserved(env, monkeypatch) -> None:
    """回归护栏：cached / audio_path 这类 AgentState 未声明的键不能被吞掉。

    改造前 scheduler 直调 agent 并读取这些键；若外壳把它们丢掉，产出物会静默缺字段。
    """

    async def rich(_state: AgentState) -> dict[str, object]:
        return {
            "final_response": "x",
            "cached": True,
            "audio_path": "/data/a.mp3",
            "morning_persisted": True,
        }

    monkeypatch.setattr(task_runner, "get_agent", lambda name: _fake_spec(rich))

    result = await task_runner.run_agent_task("t_rich", "fake", _state())

    assert result.result is not None
    assert result.result["cached"] is True
    assert result.result["audio_path"] == "/data/a.mp3"
    assert result.result["morning_persisted"] is True


@pytest.mark.asyncio
async def test_agent_task_failure_is_recorded_and_reraised(env, monkeypatch) -> None:
    async def boom(_state: AgentState) -> dict[str, object]:
        raise TimeoutError("upstream 600s")

    monkeypatch.setattr(task_runner, "get_agent", lambda name: _fake_spec(boom))

    with pytest.raises(TimeoutError):
        await task_runner.run_agent_task("t_fail", "fake", _state())

    rows = await task_store.list_runs(task_id="t_fail")
    assert len(rows) == 1
    assert rows[0]["status"] == "failed"
    assert "TimeoutError" in rows[0]["error"]


@pytest.mark.asyncio
async def test_empty_result_is_degraded(env, monkeypatch) -> None:
    async def empty(_state: AgentState) -> dict[str, object]:
        return {}

    monkeypatch.setattr(task_runner, "get_agent", lambda name: _fake_spec(empty))

    result = await task_runner.run_agent_task("t_empty", "fake", _state())

    assert result.status == "degraded"
    row = await task_store.get_run(result.run_id)
    assert row is not None
    assert row["status"] == "degraded"


@pytest.mark.asyncio
async def test_retryable_agent_retries_transient_error(env, monkeypatch) -> None:
    calls = {"n": 0}

    async def flaky(_state: AgentState) -> dict[str, object]:
        calls["n"] += 1
        if calls["n"] < 2:
            raise TimeoutError("blip")
        return {"final_response": "ok"}

    monkeypatch.setattr(
        task_runner, "get_agent", lambda name: _fake_spec(flaky, retryable=True)
    )
    monkeypatch.setattr(task_runner, "_RETRY_BACKOFF", (0.0,))

    result = await task_runner.run_agent_task("t_retry", "fake", _state())

    assert calls["n"] == 2
    assert result.status == "ok"


@pytest.mark.asyncio
async def test_non_retryable_agent_does_not_retry(env, monkeypatch) -> None:
    calls = {"n": 0}

    async def boom(_state: AgentState) -> dict[str, object]:
        calls["n"] += 1
        raise TimeoutError("blip")

    monkeypatch.setattr(task_runner, "get_agent", lambda name: _fake_spec(boom))

    with pytest.raises(TimeoutError):
        await task_runner.run_agent_task("t_noretry", "fake", _state())

    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_data_task_ok_and_failure(env) -> None:
    async def body() -> Any:
        return {"ok": True}

    ok = await task_runner.run_data_task("d_ok", body)
    assert ok.status == "ok"
    row = await task_store.get_run(ok.run_id)
    assert row is not None
    assert row["agent"] is None

    async def bad() -> Any:
        raise RuntimeError("db down")

    with pytest.raises(RuntimeError):
        await task_runner.run_data_task("d_bad", bad)
    rows = await task_store.list_runs(task_id="d_bad")
    assert rows[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_thread_id_is_per_task_and_date(env, monkeypatch) -> None:
    """thread_id 必须区分任务与时点（rhythm_master 一天四次不能串到同一线程）。"""

    async def good(_state: AgentState) -> dict[str, object]:
        return {"final_response": "x"}

    monkeypatch.setattr(task_runner, "get_agent", lambda name: _fake_spec(good))

    r1 = await task_runner.run_agent_task("rhythm_master_morning", "fake", _state())
    r2 = await task_runner.run_agent_task("rhythm_master_after_close", "fake", _state())

    row1 = await task_store.get_run(r1.run_id)
    row2 = await task_store.get_run(r2.run_id)
    assert row1 is not None and row2 is not None
    assert row1["thread_id"] != row2["thread_id"]
    assert "2026-11-18" in str(row1["thread_id"])


def test_make_agent_state_sets_scheduler_trigger() -> None:
    """trigger_source=scheduler 是各 worker 落库副作用的总开关，漏设=不落库。"""
    st = task_runner.make_agent_state(intent="morning", session_id="s", report_date="2026-11-18")
    assert st["trigger_source"] == "scheduler"
    assert st["intent"] == "morning"
    assert st["report_date"] == "2026-11-18"
    assert st["messages"] == []
