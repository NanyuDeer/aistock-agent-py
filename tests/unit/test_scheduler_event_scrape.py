"""统一事件抓取中台 Task 3 — 调度注册与手动触发接口测试

验证：
- run_event_scrape(full_daily) 可被调度链调用（Task 2 回归保护）
- _run_event_scrape_job 非交易日跳过、交易日调用 run_event_scrape
- start_scheduler 注册 event_scrape_daily / event_scrape_intraday 两个 job

mock 路径说明：
- _run_event_scrape_job 在函数内 from-import run_event_scrape，
  patch import 源 aistock_agent.services.event_scraper.run_event_scrape 即生效。
- is_trading_day 在 scheduler 模块顶部 import，patch
  aistock_agent.services.scheduler.is_trading_day。
"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.event_scraper import run_event_scrape
from aistock_agent.utils.date import shanghai_today


@pytest.fixture(autouse=True)
def _reset_scheduler_singleton():
    """每个测试后清理 scheduler 单例，避免跨测试状态泄漏。"""
    yield
    try:
        from aistock_agent.services.scheduler import shutdown_scheduler

        shutdown_scheduler()
    except Exception:
        pass


@pytest.mark.asyncio
async def test_run_event_scrape_full_daily_called_from_scheduler():
    with patch(
        "aistock_agent.services.event_scraper.scrape_full_daily",
        new=AsyncMock(return_value={"persisted": 2, "deduped": 1, "error": None}),
    ):
        result = await run_event_scrape("full_daily", score_date="2026-08-12")
    assert result["persisted"] == 2


# ── _run_event_scrape_job（交易日守卫 + 透传 scrape_mode）──


@pytest.mark.asyncio
async def test_event_scrape_job_skips_non_trading_day():
    """非交易日跳过抓取，不调用 run_event_scrape。"""
    from aistock_agent.services import scheduler

    with (
        patch("aistock_agent.services.scheduler.is_trading_day", return_value=False),
        patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
        ) as mock_run,
    ):
        await scheduler._run_event_scrape_job("full_daily")
    mock_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_event_scrape_job_calls_run_event_scrape_on_trading_day():
    """交易日调用 run_event_scrape(full_daily, score_date=今天)。

    I1 回归保护（成功日志）：若代码被改回重复传 scrape_mode= 会因 TypeError
    被下方 except 吞掉，仅靠 mock_run 断言可能失真；直接断言成功日志
    event_scrape_job_done 恰好一次、失败日志 event_scrape_job_failed 未出现，
    可从日志侧立刻报警。
    """
    from aistock_agent.services import scheduler

    with (
        patch("aistock_agent.services.scheduler.is_trading_day", return_value=True),
        patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
            # 真实返回形状：run_event_scrape 返回 {"scrape_mode", "persisted", "deduped", "error"}
            return_value={"scrape_mode": "full_daily", "persisted": 1, "deduped": 0, "error": None},
        ) as mock_run,
        patch("aistock_agent.services.scheduler.logger") as mock_logger,
    ):
        await scheduler._run_event_scrape_job("full_daily")
    mock_run.assert_awaited_once_with("full_daily", score_date=shanghai_today().isoformat())
    # I1 回归保护：成功日志事件断言 + 失败日志不出现
    mock_logger.info.assert_called_once_with(
        "event_scrape_job_done",
        scrape_mode="full_daily",
        persisted=1,
        deduped=0,
        error=None,
    )
    mock_logger.exception.assert_not_called()


@pytest.mark.asyncio
async def test_event_scrape_job_passes_intraday_mode():
    """盘中档透传 scrape_mode=intraday（同步断言成功日志，防失败被吞）。"""
    from aistock_agent.services import scheduler

    with (
        patch("aistock_agent.services.scheduler.is_trading_day", return_value=True),
        patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
            return_value={"scrape_mode": "intraday", "persisted": 0, "deduped": 0, "error": None},
        ) as mock_run,
        patch("aistock_agent.services.scheduler.logger") as mock_logger,
    ):
        await scheduler._run_event_scrape_job("intraday")
    mock_run.assert_awaited_once_with("intraday", score_date=shanghai_today().isoformat())
    mock_logger.info.assert_called_once_with(
        "event_scrape_job_done",
        scrape_mode="intraday",
        persisted=0,
        deduped=0,
        error=None,
    )
    mock_logger.exception.assert_not_called()


@pytest.mark.asyncio
async def test_event_scrape_job_swallows_run_exception():
    """run_event_scrape 抛异常时 job 不向上抛（记录 event_scrape_job_failed 失败日志）。"""
    from aistock_agent.services import scheduler

    with (
        patch("aistock_agent.services.scheduler.is_trading_day", return_value=True),
        patch(
            "aistock_agent.services.event_scraper.run_event_scrape",
            new_callable=AsyncMock,
            side_effect=RuntimeError("scrape boom"),
        ),
        patch("aistock_agent.services.scheduler.logger") as mock_logger,
    ):
        await scheduler._run_event_scrape_job("full_daily")  # 不应抛异常
    # 失败日志事件断言：与 I1 成功日志断言成对，防止异常被静默吞掉
    mock_logger.exception.assert_called_once_with(
        "event_scrape_job_failed",
        scrape_mode="full_daily",
        error="scrape boom",
    )


# ── start_scheduler 注册集成检查 ──


@pytest.mark.asyncio
async def test_start_scheduler_registers_event_scrape_jobs():
    """start_scheduler 注册 event_scrape_daily / event_scrape_intraday 两个 job。"""
    from aistock_agent.services.scheduler import (
        get_scheduler,
        shutdown_scheduler,
        start_scheduler,
    )

    start_scheduler()
    scheduler = get_scheduler()
    job_ids = [j.id for j in scheduler.get_jobs()]
    assert "event_scrape_daily" in job_ids
    assert "event_scrape_intraday" in job_ids
    shutdown_scheduler()
