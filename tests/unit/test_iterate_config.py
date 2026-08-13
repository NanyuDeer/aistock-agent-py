"""iterate 配置默认值 —— 迭代闭环默认关闭，环境变量可开启"""

import pytest


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("iterate_enabled", False),
        ("iterate_data_dir", "data"),
        ("iterate_cron", "0 17 * * 1-5"),
        ("iterate_case_build_cron", "30 16 * * 1-5"),
        ("iterate_max_rounds", 5),
        ("iterate_target_score", 0.8),
        ("iterate_max_daily_cases", 3),
        ("iterate_round_timeout_seconds", 600),
        ("iterate_smtp_port", 465),
    ],
)
def test_iterate_config_defaults(field: str, expected: object) -> None:
    from aistock_agent.config import Settings

    settings = Settings(_env_file=None)
    assert getattr(settings, field) == expected
