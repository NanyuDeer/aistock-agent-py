"""统一事件抓取中台 Task 3 — 调度注册与手动触发接口测试

验证：
- run_event_scrape(full_daily) 可被调度链调用（Task 2 回归保护）
- _run_event_scrape_job 非交易日跳过、交易日调用 run_event_scrape
- start_scheduler 注册 event_scrape_daily / event_scrape_intraday 两个 job
- H5（Task 3，2026-08-13）：早间 08:45（intraday）与收盘 15:05（full_daily）
  两个档位已注册（config 默认值 + job id event_scrape_early / event_scrape_close）

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


# ── I4 兜底放宽（H7，Task 2 / Phase-3b）──


@pytest.mark.asyncio
async def test_morning_fallback_triggers_when_conduction_missing_and_not_marked():
    """I4 放宽：库有数据但无当日传导报告且未被标记 → 晨报降级触发。

    旧条件仅"库空"触发；放宽后"库空 或 无当日传导报告"且未被中台标记
    （conduction_triggered:{date} 不存在）即触发，防"抓取成功但传导失败"静默缺失。
    """
    import asyncio

    from aistock_agent.services.scheduler import _run_morning_task

    with (
        patch("aistock_agent.services.scheduler.is_trading_day", return_value=True),
        # morning_agent 在函数内 import，patch import 源模块（test_scheduler.py 既有约定）
        patch("aistock_agent.agents.workers.morning", create=True) as mock_agent,
        # load_event_scrape 在 I4 块内函数级 import，patch 源模块
        patch(
            "aistock_agent.services.event_store.load_event_scrape",
            new=AsyncMock(return_value=[{"event_id": "e1"}]),
        ),
        # node_api 为 scheduler 模块级 import（scheduler.py:25），patch 模块属性生效
        patch(
            "aistock_agent.services.scheduler.node_api.list_analysis_reports",
            new=AsyncMock(return_value=[]),
        ),
        # RedisPool 在 I4 块内函数级 import，patch 源模块
        patch("aistock_agent.services.redis_pool.RedisPool") as mock_pool,
        patch(
            "aistock_agent.services.scheduler._run_event_analysis_pipeline_task",
            new=AsyncMock(),
        ) as mock_task,
    ):
        mock_agent.run = AsyncMock(
            return_value={
                "final_response": "x",
                "analysis_reports": {"major_events": [{"event_id": "e1"}]},
            }
        )
        fake_client = AsyncMock()
        fake_client.get = AsyncMock(return_value=None)  # 未标记
        mock_pool.get_client = AsyncMock(return_value=fake_client)
        await _run_morning_task()
        # fire-and-forget：等待后台 task 完成再断言（对齐既有 I4 测试先例）
        await asyncio.sleep(0.1)
    mock_task.assert_awaited_once()


# ── H5 五窗补齐（Task 3，2026-08-13）：早间 08:45 + 收盘 15:05 ──


def test_event_scrape_jobs_registered_include_early_and_close():
    """H5：早间刷新与收盘汇总档位已注册（config 默认值）。"""
    from aistock_agent.config import settings

    assert settings.scheduler_event_scrape_early_cron == "45 8 * * 1-5"
    assert settings.scheduler_event_scrape_close_cron == "5 15 * * 1-5"


@pytest.mark.asyncio
async def test_scheduler_registers_early_and_close_jobs():
    from aistock_agent.services.scheduler import (
        _run_event_scrape_job,
        get_scheduler,
        start_scheduler,
    )

    start_scheduler()
    try:
        ids = {job.id for job in get_scheduler().get_jobs()}
        assert "event_scrape_early" in ids
        assert "event_scrape_close" in ids
    finally:
        get_scheduler().remove_all_jobs()
