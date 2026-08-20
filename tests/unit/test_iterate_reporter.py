"""reporter —— 每日汇总报告构建与 SMTP 发送（复用 mail_sender）"""

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
    """发送成功（mail_sender 返回 True）→ 返回 True 且不写兜底。"""
    with patch(
        "aistock_agent.iterate.reporter.send_mail", return_value=True
    ) as mock_send:
        ok = send_report_via_smtp("# 迭代报告", subject="iterate daily")
    assert ok is True
    mock_send.assert_called_once()
    # 正文是 HTML <pre> 包裹的转义 Markdown
    body_html = mock_send.call_args.args[1]
    assert body_html.startswith("<pre") and "# 迭代报告" in body_html


def test_send_report_via_smtp_failure_writes_fallback(
    smtp_settings: object, iterate_data_dir: object
) -> None:
    """mail_sender 发送失败（返回 False）→ 返回 False 且写 data/reports/ 兜底。"""
    from datetime import date

    reports_dir = Path(iterate_data_dir) / "reports"  # type: ignore[arg-type]
    with patch(
        "aistock_agent.iterate.reporter.send_mail", return_value=False
    ) as mock_send:
        ok = send_report_via_smtp("# 迭代报告", subject="iterate daily")
    assert ok is False
    mock_send.assert_called_once()
    fallback = reports_dir / f"{date.today().isoformat()}.md"
    assert fallback.exists()
    assert "# 迭代报告" in fallback.read_text(encoding="utf-8")


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
async def test_build_daily_report_excludes_best_summary(
    iterate_data_dir: object,
) -> None:
    """_best.json（补丁固化汇总，无 case_id/created_at）不得混入实验汇总。

    2026-08-14 事故：8-14 报告显示"空案例 r1 baseline 0.75"——best.json
    无 created_at 被"旧记录恒包含"逻辑带进汇总，而真正的 r1/r2/r3
    （created_at=8-13）被日期过滤掉。
    """
    import json
    from datetime import date
    from pathlib import Path

    root = Path(iterate_data_dir) / "experiments"  # type: ignore[arg-type]
    root.mkdir(parents=True, exist_ok=True)
    (root / "case_20260814_review_今日_best.json").write_text(
        json.dumps({"score": 0.75, "round": 1, "patch": {}}), encoding="utf-8"
    )
    (root / "case_20260814_r1.json").write_text(
        json.dumps(
            {
                "case_id": "case_20260814",
                "created_at": date.today().isoformat(),
                "score": 0.5,
                "round": 1,
            }
        ),
        encoding="utf-8",
    )

    md = await build_daily_report()
    assert "0.75" not in md  # best 汇总不显示
    assert "case_20260814" in md


def test_format_improvements_shows_patch_summary() -> None:
    """改进建议展示变体 patch 摘要（old→new），让负责人知道"改了什么"。

    patch 是实验记录顶层字段（run_experiment_round 写入结构：variant 只含
    type/files/instructions；patch 含 target_symbol/old_snippet/new_snippet）。
    """
    from aistock_agent.iterate.reporter import _format_improvements

    experiments = [
        {
            "case_id": "case_a",
            "round": 2,
            "variant": {
                "type": "prompt_diff",
                "files": ["src/aistock_agent/prompts/workers/review.py"],
                "instructions": "增加外盘传导优先指令",
            },
            "patch": {
                "target_symbol": "REVIEW_PROMPT",
                "old_snippet": "【调查规则】\n1. primary 是唯一归因对象",
                "new_snippet": "【调查规则】\n1. primary 是唯一归因对象\n2. 强制列出板块清单",
            },
            "score": 0.7,
        },
    ]
    out = _format_improvements(experiments)
    assert "增加外盘传导优先指令" in out
    assert "patch:" in out
    assert "→" in out  # old → new 摘要标记
    assert "【调查规则】" in out  # patch 内容可见


@pytest.mark.asyncio
async def test_run_daily_report_attaches_experiments(
    iterate_data_dir: object,
) -> None:
    """报告附带当日实验记录 JSON 附件（用户可查看完整轮次/patch 规格）。"""
    import json
    from datetime import date
    from pathlib import Path

    root = Path(iterate_data_dir) / "experiments"  # type: ignore[arg-type]
    root.mkdir(parents=True, exist_ok=True)
    (root / "case_20260814_r1.json").write_text(
        json.dumps(
            {
                "case_id": "case_20260814",
                "created_at": date.today().isoformat(),
                "round": 1,
                "score": 0.5,
            }
        ),
        encoding="utf-8",
    )
    from aistock_agent.iterate.reporter import run_daily_report

    with patch(
        "aistock_agent.iterate.reporter.send_report_via_smtp", return_value=True
    ) as mock_send:
        await run_daily_report(date.today())
    attachments = mock_send.call_args.kwargs.get("attachments") or ()
    assert len(attachments) == 1  # 当日实验记录附件
    assert Path(attachments[0]).exists()


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
