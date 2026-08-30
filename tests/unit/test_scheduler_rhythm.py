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


@pytest.mark.asyncio
@pytest.mark.usefixtures("_reset_scheduler_singleton")
async def test_rhythm_jobs_registered() -> None:
    # 注：job 注册发生在 start_scheduler() 内（get_scheduler() 仅懒创建单例），
    # 与 test_scheduler.py::test_start_scheduler_initializes_jobs 同模式。
    scheduler.start_scheduler()
    sched = scheduler.get_scheduler()
    try:
        job_ids = {job.id for job in sched.get_jobs()}
        assert "rhythm_master_after_close" in job_ids
        assert "rhythm_master_morning" in job_ids
        assert "rhythm_master_midday" in job_ids
    finally:
        # APScheduler 3.10 AsyncIOScheduler.shutdown 为同步方法（无 await）
        sched.shutdown(wait=False)
