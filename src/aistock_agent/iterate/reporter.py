"""每日汇总报告 —— 聚合实验结果 + 待标注清单 + 系统状态，SMTP 发到 QQ 邮箱。"""

import json
import smtplib
import time
from datetime import date
from email.header import Header
from email.mime.text import MIMEText
from typing import cast

import structlog

from aistock_agent.config import settings
from aistock_agent.iterate.case_builder import get_data_dir
from aistock_agent.iterate.ground_truth import list_pending_review

logger = structlog.get_logger()

_SMTP_RETRIES = 3
_SMTP_RETRY_DELAY_SECONDS = 2


async def build_daily_report(report_date: date | None = None) -> str:
    """构建每日汇总 Markdown。无重要结果也发（设计文档 9.1）。"""
    day = report_date or date.today()
    experiments = _read_experiments()
    pending = list_pending_review()

    lines = [
        f"# 迭代 Agent 每日汇总报告（{day.isoformat()}）",
        "",
        "## 一、当日迭代实验汇总",
        _format_experiments(experiments),
        "",
        "## 二、改进建议",
        _format_improvements(experiments),
        "",
        "## 三、待标注案例清单",
        _format_pending(pending),
        "",
        "## 四、系统状态",
        _format_system_status(),
        "",
    ]
    return "\n".join(lines)


def send_report_via_smtp(markdown: str, *, subject: str) -> bool:
    """SMTP 发送；失败重试 3 次；最终失败写 data/reports/ 兜底。"""
    if not (settings.iterate_smtp_host and settings.iterate_smtp_user and settings.iterate_mail_to):
        logger.warning("iterate_smtp_not_configured")
        return False

    msg = MIMEText(markdown, "plain", "utf-8")
    msg["Subject"] = cast("str", Header(subject, "utf-8"))
    msg["From"] = settings.iterate_smtp_user
    msg["To"] = settings.iterate_mail_to

    for attempt in range(1, _SMTP_RETRIES + 1):
        try:
            with smtplib.SMTP_SSL(
                settings.iterate_smtp_host, settings.iterate_smtp_port, timeout=15
            ) as server:
                server.login(settings.iterate_smtp_user, settings.iterate_smtp_password)
                server.sendmail(
                    settings.iterate_smtp_user,
                    [settings.iterate_mail_to],
                    msg.as_string(),
                )
            logger.info("iterate_report_sent", subject=subject)
            return True
        except (OSError, smtplib.SMTPException) as exc:  # noqa: PERF203
            logger.warning("iterate_report_smtp_failed", attempt=attempt, error=str(exc))
            if attempt < _SMTP_RETRIES:
                time.sleep(_SMTP_RETRY_DELAY_SECONDS)

    _write_report_fallback(markdown)
    return False


async def run_daily_report() -> None:
    """构建 + 发送每日报告（scheduler 调用）。"""
    md = await build_daily_report()
    subject = f"迭代Agent每日汇总 {date.today().isoformat()}"
    ok = send_report_via_smtp(md, subject=subject)
    if not ok:
        logger.error("iterate_report_final_failure", subject=subject)


def _read_experiments() -> list[dict[str, object]]:
    root = get_data_dir() / "experiments"
    if not root.exists():
        return []
    records: list[dict[str, object]] = []
    for p in sorted(root.glob("*.json")):
        try:
            records.append(json.loads(p.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            continue
    return records


def _format_experiments(experiments: list[dict[str, object]]) -> str:
    if not experiments:
        return "（今日无实验）"
    lines = ["| 案例 | 轮次 | 变体 | 评分 | 差距分析 |", "|---|---|---|---|---|"]
    for e in experiments:
        variant = e.get("variant", {})
        vtype = variant.get("type", "baseline") if isinstance(variant, dict) else "baseline"
        lines.append(
            f"| {e.get('case_id', '')} | r{e.get('round', '')} | {vtype} "
            f"| {e.get('score', 0)} | {e.get('gap_analysis', '')} |"
        )
    return "\n".join(lines)


def _format_improvements(experiments: list[dict[str, object]]) -> str:
    if not experiments:
        return "（无改进建议）"
    lines: list[str] = []
    for e in experiments:
        variant = e.get("variant", {})
        if isinstance(variant, dict) and variant.get("type") != "baseline":
            lines.append(
                f"- 案例 {e.get('case_id', '')} r{e.get('round', '')}："
                f"改动 {variant.get('files', [])}，评分 {e.get('score', 0)}，"
                f"建议：{variant.get('instructions', '')}"
            )
    return "\n".join(lines) if lines else "（无改进建议）"


def _format_pending(pending: list[dict[str, object]]) -> str:
    if not pending:
        return "（无待标注案例）"
    lines = [
        "以下案例标准答案置信度低，请在 DeepSeek 网页版辅助标注后按模板回填"
        " data/ground_truths/ 对应 JSON：",
        "",
    ]
    for p in pending:
        case = p.get("case_id", "")
        lines.append(f"- `{case}`：gt_id=`{p.get('gt_id', '')}`")
    lines.append(
        '\n回填模板：{"gt_id": "<原gt_id>", "case_id": "<原case_id>", '
        '"confidence": "high|medium|low", '
        '"attribution": {"direction": "bullish|bearish|neutral", '
        '"drivers": [...], "transmission_path": [...], '
        '"affected_sectors": [...], "source_notes": [...]}}'
    )
    return "\n".join(lines)


def _format_system_status() -> str:
    from aistock_agent.iterate.case_builder import list_cases

    cases = list_cases()
    pending = list_pending_review()
    exps = _read_experiments()
    return (
        f"- 切片库：{len(cases)} 个案例\n"
        f"- 标准答案库：待标注 {len(pending)} 条\n"
        f"- 实验记录：{len(exps)} 条"
    )


def _write_report_fallback(markdown: str) -> None:
    root = get_data_dir() / "reports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{date.today().isoformat()}.md"
    path.write_text(markdown, encoding="utf-8")
    logger.info("iterate_report_written_fallback", path=str(path))
