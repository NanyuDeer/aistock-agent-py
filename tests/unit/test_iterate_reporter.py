"""reporter —— 每日汇总报告构建与 SMTP 发送（重试 3 次）"""

from collections.abc import Iterator
from pathlib import Path
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


@pytest.mark.asyncio
async def test_build_daily_report_filters_experiments_by_date(
    iterate_data_dir: object,
) -> None:
    """I5 回归：报告只展示当日（created_at == 报告日期）实验；无 created_at 旧记录恒包含。"""
    import json
    from datetime import date, timedelta
    from pathlib import Path

    root = Path(iterate_data_dir) / "experiments"  # type: ignore[arg-type]
    root.mkdir(parents=True, exist_ok=True)
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    (root / "case_today_r1.json").write_text(
        json.dumps({"case_id": "case_today", "created_at": date.today().isoformat()}),
        encoding="utf-8",
    )
    (root / "case_yesterday_r1.json").write_text(
        json.dumps({"case_id": "case_yesterday", "created_at": yesterday}),
        encoding="utf-8",
    )
    (root / "case_legacy_r1.json").write_text(
        json.dumps({"case_id": "case_legacy"}), encoding="utf-8"
    )

    md = await build_daily_report()
    assert "case_today" in md
    assert "case_yesterday" not in md
    assert "case_legacy" in md  # 无 created_at 的旧记录向后兼容包含


@pytest.mark.asyncio
async def test_build_daily_report_empty_store_notes_no_pending_cases(tmp_path: Path) -> None:
    """I4 回归：空切片库时报告注明"无待迭代案例"，不报错。"""
    from aistock_agent.config import settings

    original = settings.iterate_data_dir
    settings.iterate_data_dir = str(tmp_path)
    try:
        md = await build_daily_report()
        assert "无待迭代案例" in md
    finally:
        settings.iterate_data_dir = original
