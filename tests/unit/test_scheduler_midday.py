"""调度器盘中报接入测试（交易日守卫 + midday_briefing job 注册 + 串行信号量）。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.config import settings


def test_scheduler_midday_cron_config():
    # 从配置读取，避免硬编码
    assert settings.scheduler_midday_cron == "5 12 * * 0-4"
    # 验证非整点（错开 12:00 事件抓取）
    minutes, hours = settings.scheduler_midday_cron.split()[0], settings.scheduler_midday_cron.split()[1]
    assert minutes != "0"


@pytest.mark.asyncio
async def test_run_midday_task_skips_non_trading_day():
    from aistock_agent.services import scheduler as sched_mod

    with patch.object(sched_mod, "is_trading_day", return_value=False):
        result = await sched_mod._run_midday_task(report_date="2026-08-23")
    assert result["reason"] == "non_trading_day" or result.get("midday_generated") is False


@pytest.mark.asyncio
async def test_run_midday_task_invokes_worker():
    from aistock_agent.services import scheduler as sched_mod

    worker_run_mock = AsyncMock(return_value={"final_response": "{\"schema_version\":\"2.0\"}"})
    with (
        patch.object(sched_mod, "is_trading_day", return_value=True),
        patch("aistock_agent.agents.workers.midday.run", worker_run_mock),
    ):
        result = await sched_mod._run_midday_task(report_date="2026-08-24")
    worker_run_mock.assert_awaited_once()
    assert result["status"] in ("ok", "partial")


@pytest.mark.asyncio
async def test_midday_ai_semaphore_limits_concurrency_to_1():
    from aistock_agent.services.scheduler import _midday_llm_semaphore
    # 默认 Semaphore(1) 证明存在且值为1
    assert _midday_llm_semaphore._value == 1