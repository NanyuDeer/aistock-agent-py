"""scheduler 日历 job 注册（裁决 C7）：种子/抓取/预期差三班。"""
import inspect

from aistock_agent.config import settings


def test_calendar_cron_config_exists():
    assert settings.scheduler_calendar_seed_cron == "30 7 * * 0-4"
    assert settings.scheduler_calendar_scrape_cron == "40 7 * * 0-4"
    assert settings.scheduler_expectation_diff_cron == "0 8 * * 0-4"
    assert "11,13" in settings.scheduler_expectation_diff_intraday_cron


def test_scheduler_registers_jobs():
    import aistock_agent.services.scheduler as mod  # noqa: PLC0415

    # 验证 start_scheduler 内 add_job 的 id 集合含日历三 job
    src = inspect.getsource(mod.start_scheduler)
    for jid in (
        "calendar_seed_import",
        "calendar_scrape",
        "expectation_diff_morning",
        "expectation_diff_intraday",
    ):
        assert jid in src
