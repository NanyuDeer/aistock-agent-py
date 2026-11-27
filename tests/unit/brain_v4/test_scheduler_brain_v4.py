"""V4 影子定时调度（P5）：开关、注册、交易日守卫、重试与幂等归档。

为什么这样测：
- 用记录 add_job 调用的假 scheduler，直接断言"注册了什么"，不启动真实
  AsyncIOScheduler（否则会拉起后台 loop，测试相互污染）；
- 用 monkeypatch 替换 ``run_live`` / ``archive_run``，保证不触网、不真实取数；
- 幂等语义只看 ``status``：runner 返回 already_exists 时不得再次归档。
"""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from apscheduler.triggers.cron import CronTrigger

from aistock_agent.config import settings
from aistock_agent.services import scheduler as sched


class _FakeScheduler:
    """只记录 add_job 入参的假调度器。"""

    def __init__(self) -> None:
        self.jobs: list[dict[str, object]] = []

    def add_job(self, func: object, trigger: object, **kwargs: object) -> None:
        self.jobs.append({"func": func, "trigger": trigger, **kwargs})


def _fake_result(*, status: str = "ok") -> MagicMock:
    result = MagicMock()
    result.status = status
    result.run_id = "v4_run_1"
    result.profile = "market_trace"
    result.payload = {"run_id": "v4_run_1"}
    return result


def test_config_defaults() -> None:
    """默认全关 + 执行包给定的 cron/超时/重试值。"""
    assert settings.brain_v4_enabled is False
    assert settings.brain_v4_shadow_only is True
    assert settings.brain_v4_morning_cron == "51 8 * * 1-5"
    assert settings.brain_v4_midday_cron == "6 12 * * 1-5"
    assert settings.brain_v4_evening_cron == "36 15 * * 1-5"
    assert settings.brain_v4_market_trace_cron == "37 15 * * 1-5"
    assert settings.brain_v4_timeout_s == 900
    assert settings.brain_v4_retry == 1


def test_disabled_registers_no_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "brain_v4_enabled", False, raising=False)
    fake = _FakeScheduler()

    sched._register_brain_v4_jobs(fake)  # type: ignore[arg-type]

    assert fake.jobs == []


def test_enabled_registers_exactly_four_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "brain_v4_enabled", True, raising=False)
    fake = _FakeScheduler()

    sched._register_brain_v4_jobs(fake)  # type: ignore[arg-type]

    assert len(fake.jobs) == 4
    ids = [str(job["id"]) for job in fake.jobs]
    assert len(set(ids)) == 4
    assert all(job_id.startswith("brain_v4_") for job_id in ids)
    for job in fake.jobs:
        # 影子任务长时间占用数据/LLM 资源：不允许重叠执行；loop 短暂繁忙不跳过。
        assert job["max_instances"] == 1
        assert job["misfire_grace_time"] == 600
        assert job["replace_existing"] is True
        assert job["kwargs"] == {"profile": _PROFILE_BY_ID[str(job["id"])]}


_PROFILE_BY_ID = {
    "brain_v4_market_trace": "market_trace",
    "brain_v4_morning": "briefing_morning",
    "brain_v4_midday": "briefing_midday",
    "brain_v4_evening": "briefing_evening",
}


def test_enabled_job_cron_reads_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """cron 必须来自 settings（而非硬编码），改配置即生效。"""
    monkeypatch.setattr(settings, "brain_v4_enabled", True, raising=False)
    custom = {
        "brain_v4_market_trace_cron": "1 7 * * 1-5",
        "brain_v4_morning_cron": "2 8 * * 1-5",
        "brain_v4_midday_cron": "3 12 * * 1-5",
        "brain_v4_evening_cron": "4 15 * * 1-5",
    }
    for name, value in custom.items():
        monkeypatch.setattr(settings, name, value, raising=False)

    fake = _FakeScheduler()
    sched._register_brain_v4_jobs(fake)  # type: ignore[arg-type]

    cron_by_id = {
        "brain_v4_market_trace": custom["brain_v4_market_trace_cron"],
        "brain_v4_morning": custom["brain_v4_morning_cron"],
        "brain_v4_midday": custom["brain_v4_midday_cron"],
        "brain_v4_evening": custom["brain_v4_evening_cron"],
    }
    for job in fake.jobs:
        job_id = str(job["id"])
        expected = CronTrigger.from_crontab(
            cron_by_id[job_id], timezone=settings.scheduler_timezone
        )
        assert str(job["trigger"]) == str(expected)


@pytest.mark.asyncio
async def test_non_trading_day_skips_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sched, "is_trading_day", lambda _day: False)
    runner = AsyncMock()

    with patch("aistock_agent.brain_v4.runtime.runner.run_live", runner):
        await sched._run_brain_v4_task("market_trace")

    runner.assert_not_awaited()


@pytest.mark.asyncio
async def test_trading_day_runs_profile_and_archives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sched, "is_trading_day", lambda _day: True)
    runner = AsyncMock(return_value=_fake_result())
    archiver = AsyncMock()

    with (
        patch("aistock_agent.brain_v4.runtime.runner.run_live", runner),
        patch("aistock_agent.brain_v4.runtime.archive.archive_run", archiver),
    ):
        await sched._run_brain_v4_task("briefing_morning")

    runner.assert_awaited_once()
    assert runner.await_args is not None
    assert runner.await_args.args[0] == "briefing_morning"
    assert isinstance(runner.await_args.args[1], date)
    archiver.assert_awaited_once()


@pytest.mark.asyncio
async def test_retry_once_then_gives_up_without_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """首次失败重试一次（共 2 次调用）；再失败只记录，不向上抛。"""
    monkeypatch.setattr(sched, "is_trading_day", lambda _day: True)
    runner = AsyncMock(side_effect=RuntimeError("boom"))
    archiver = AsyncMock()

    with (
        patch("aistock_agent.brain_v4.runtime.runner.run_live", runner),
        patch("aistock_agent.brain_v4.runtime.archive.archive_run", archiver),
    ):
        await sched._run_brain_v4_task("market_trace")

    assert runner.await_count == 2
    archiver.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_exists_skips_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sched, "is_trading_day", lambda _day: True)
    runner = AsyncMock(return_value=_fake_result(status="already_exists"))
    archiver = AsyncMock()

    with (
        patch("aistock_agent.brain_v4.runtime.runner.run_live", runner),
        patch("aistock_agent.brain_v4.runtime.archive.archive_run", archiver),
    ):
        await sched._run_brain_v4_task("market_trace")

    runner.assert_awaited_once()
    archiver.assert_not_awaited()


@pytest.mark.asyncio
async def test_archive_failure_does_not_break_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sched, "is_trading_day", lambda _day: True)
    runner = AsyncMock(return_value=_fake_result())
    archiver = AsyncMock(side_effect=RuntimeError("archive down"))

    with (
        patch("aistock_agent.brain_v4.runtime.runner.run_live", runner),
        patch("aistock_agent.brain_v4.runtime.archive.archive_run", archiver),
    ):
        # 归档失败不得抛出（否则 APScheduler 记 job 异常，掩盖真实研判成功）
        await sched._run_brain_v4_task("market_trace")

    runner.assert_awaited_once()
    archiver.assert_awaited_once()
