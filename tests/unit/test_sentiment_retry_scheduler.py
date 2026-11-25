"""情绪温度调度重试：快照 not_ready 时按期重试，全失败才放弃。"""

import asyncio

import pytest

from aistock_agent.services import scheduler


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """避免真实等待 10/20 分钟。"""
    waits: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        waits.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return waits


@pytest.mark.asyncio
async def test_retries_until_snapshot_ready(monkeypatch, _no_real_sleep):
    calls: list[str] = []

    async def fake_compute(target: str, *args, **kwargs):
        calls.append(target)
        if len(calls) < 2:
            return None  # 第一次快照未就绪
        return {"date": target, "score": 45.0, "ice": {"is_ice": False}}

    monkeypatch.setattr(
        "aistock_agent.services.sentiment_temp.compute_and_persist_sentiment_temp",
        fake_compute,
    )

    await scheduler._run_sentiment_temp_task()

    assert len(calls) == 2
    assert _no_real_sleep == [600]  # 只在重试前等一次


@pytest.mark.asyncio
async def test_gives_up_after_all_attempts(monkeypatch, _no_real_sleep):
    calls: list[str] = []

    async def fake_compute(target: str, *args, **kwargs):
        calls.append(target)
        return None

    monkeypatch.setattr(
        "aistock_agent.services.sentiment_temp.compute_and_persist_sentiment_temp",
        fake_compute,
    )

    await scheduler._run_sentiment_temp_task()

    assert len(calls) == 3  # 0 / 10 / 20 分钟共三次
    assert _no_real_sleep == [600, 1200]


@pytest.mark.asyncio
async def test_exception_does_not_abort_remaining_attempts(monkeypatch, _no_real_sleep):
    calls: list[str] = []

    async def fake_compute(target: str, *args, **kwargs):
        calls.append(target)
        if len(calls) == 1:
            raise RuntimeError("node api exploded")
        return {"date": target, "score": 50.0, "ice": {"is_ice": False}}

    monkeypatch.setattr(
        "aistock_agent.services.sentiment_temp.compute_and_persist_sentiment_temp",
        fake_compute,
    )

    await scheduler._run_sentiment_temp_task()

    assert len(calls) == 2
