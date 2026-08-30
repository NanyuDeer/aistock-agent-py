"""scheduler 三时点注册（§8/D13）：16:05 收盘基准 + 9:00 盘前 + 12:30 午间。"""
import pytest

from aistock_agent.services import scheduler


@pytest.fixture(autouse=True)
def _reset_scheduler_singleton():
    """每个测试后清理 scheduler 单例，避免跨测试状态泄漏（对齐 test_scheduler.py）。"""
    yield
    try:
        scheduler.shutdown_scheduler()
    except Exception:
        pass


def test_rhythm_jobs_registered() -> None:
    """scheduler 三时点注册（§8/D13）。同步 + 显式 loop 管理，避免全量套件下
    asyncio 缓存 loop 已关闭导致 'Event loop is closed'（对齐 test_scheduler.py 同步测试先例）。"""
    import asyncio
    from unittest.mock import patch

    # 前置 async 测试（如 test_scheduler_event_scrape::test_scheduler_registers_early_and_close_jobs
    # 只 remove_all_jobs 不 shutdown）依赖 fixture teardown 清理，但 pytest-asyncio 先关 loop，
    # teardown 里 shutdown_scheduler 抛异常被吞 → 残留绑定已关闭 loop 的单例。
    # get_scheduler() 会返回该残留实例（state=RUNNING），add_job→wakeup→call_soon_threadsafe
    # 即抛 'Event loop is closed'，故先强制丢弃。
    scheduler._scheduler = None

    # AsyncIOScheduler.start() 需要 running event loop（Python 3.13 get_running_loop）：
    # 同步测试里打桩为 no-op 即可，job 注册全部在 start() 之前完成，不影响断言语义。
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        with patch.object(scheduler.AsyncIOScheduler, "start"):
            scheduler.start_scheduler()
        sched = scheduler.get_scheduler()
        job_ids = {job.id for job in sched.get_jobs()}
        assert "rhythm_master_after_close" in job_ids
        assert "rhythm_master_morning" in job_ids
        assert "rhythm_master_midday" in job_ids
    finally:
        scheduler.shutdown_scheduler()
        loop.close()
        asyncio.set_event_loop(None)
