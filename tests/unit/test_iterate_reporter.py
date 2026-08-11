"""reporter —— 每日汇总报告构建与 SMTP 发送（重试 3 次）"""

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from aistock_agent.iterate.reporter import build_daily_report, send_report_via_smtp


@pytest.fixture
def smtp_settings() -> Iterator[None]:
    """填充 SMTP 配置，否则 send_report_via_smtp 会走未配置守卫直接返回 False。"""
    from aistock_agent.config import settings

    original = (
        settings.iterate_smtp_host,
        settings.iterate_smtp_user,
        settings.iterate_mail_to,
    )
    settings.iterate_smtp_host = "smtp.example.com"
    settings.iterate_smtp_user = "noreply@example.com"
    settings.iterate_mail_to = "ops@example.com"
    try:
        yield
    finally:
        (
            settings.iterate_smtp_host,
            settings.iterate_smtp_user,
            settings.iterate_mail_to,
        ) = original


def test_send_report_via_smtp_success(smtp_settings: object) -> None:
    with patch("aistock_agent.iterate.reporter.smtplib.SMTP_SSL") as mock_smtp:
        ok = send_report_via_smtp("# 迭代报告", subject="iterate daily")
    assert ok is True
    mock_smtp.assert_called_once()


def test_send_report_via_smtp_retries_then_fails(
    smtp_settings: object, iterate_data_dir: object
) -> None:
    with patch(
        "aistock_agent.iterate.reporter.smtplib.SMTP_SSL", side_effect=OSError("conn")
    ) as mock_smtp:
        ok = send_report_via_smtp("# 迭代报告", subject="iterate daily")
    assert ok is False
    assert mock_smtp.call_count == 3  # 重试 3 次


@pytest.mark.asyncio
async def test_build_daily_report_contains_pending(iterate_data_dir: object) -> None:
    md = await build_daily_report()
    assert "待标注" in md
    assert "gt_pending_low_confidence" in md
