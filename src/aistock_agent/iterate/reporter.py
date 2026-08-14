"""每日汇总报告 —— 聚合实验结果 + 待标注清单 + 系统状态，SMTP 发到 QQ 邮箱。

SMTP 发送复用 services/mail_sender（QQ 邮箱已验证模式：SSL + 授权码 + HTML 正文），
报告以 HTML <pre> 正文发送，保留格式可读性。
"""

import html
import json
from datetime import date

import structlog

from aistock_agent.iterate.case_builder import get_data_dir
from aistock_agent.iterate.ground_truth import list_pending_review
from aistock_agent.services.mail_sender import send_mail

logger = structlog.get_logger()


async def build_daily_report(report_date: date | None = None) -> str:
    """构建每日汇总 Markdown。无重要结果也发（设计文档 9.1）。"""
    day = report_date or date.today()
    experiments = _read_experiments(day)
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


def send_report_via_smtp(
    markdown: str, *, subject: str, attachments: tuple[str, ...] = ()
) -> bool:
    """SMTP 发送（HTML 正文 + 可选附件）；重试与配置解析由 mail_sender 负责；最终失败写兜底。"""
    body_html = (
        "<pre style='font-family:Menlo,Consolas,monospace;font-size:12px;"
        f"white-space:pre-wrap'>{html.escape(markdown)}</pre>"
    )
    ok = send_mail(subject, body_html, attachments=attachments)
    if not ok:
        _write_report_fallback(markdown)
    return ok


async def run_daily_report(report_date: date | None = None) -> None:
    """构建 + 发送每日报告（scheduler 调用）。

    report_date：手动补发历史日期的报告（2026-08-14 用户需求：主应用
    scheduler 未运行时无自动发送，--once --date 补发）。
    附件：当日实验记录 JSON（完整轮次/patch 规格，2026-08-14 用户反馈
    邮件正文信息不足——只想看"改了什么"需要完整补丁）。
    """
    md = await build_daily_report(report_date)
    day = report_date or date.today()
    subject = f"迭代Agent每日汇总 {day.isoformat()}"
    ok = send_report_via_smtp(
        md, subject=subject, attachments=_collect_experiment_attachments(day)
    )
    if not ok:
        logger.error("iterate_report_final_failure", subject=subject)


def _collect_experiment_attachments(day: date) -> tuple[str, ...]:
    """当日实验记录合并 JSON（含完整 patch 规格），作为报告附件。"""
    records = _read_experiments(day)
    if not records:
        return ()
    root = get_data_dir() / "reports"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{day.isoformat()}_experiments.json"
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("iterate_report_attachment_written", path=str(path), count=len(records))
    return (str(path),)


def _read_experiments(report_date: date | None = None) -> list[dict[str, object]]:
    """读取实验记录；report_date 非空时只保留 created_at 日期 == report_date 的记录。

    向后兼容：无 created_at 字段的旧记录（本修复前写入）视为"当日"恒包含，
    避免历史实验从报告中消失；有 created_at 的记录按 ISO 日期精确过滤。
    排除 ``_best.json``（补丁固化汇总，无 case_id/created_at，2026-08-14 事故：
    被"旧记录恒包含"误带进实验汇总，显示空案例行）。
    """
    root = get_data_dir() / "experiments"
    if not root.exists():
        return []
    records: list[dict[str, object]] = []
    day = report_date.isoformat() if report_date else None
    for p in sorted(root.glob("*.json")):
        if p.name.endswith("_best.json"):
            continue  # 补丁固化汇总文件，非轮次实验记录
        try:
            record = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        created_at = record.get("created_at")
        if day is not None and isinstance(created_at, str) and created_at != day:
            continue
        records.append(record)
    return records


def _format_experiments(experiments: list[dict[str, object]]) -> str:
    if not experiments:
        # 空切片库（无待迭代案例）与"今日无实验"区分展示，避免误读
        from aistock_agent.iterate.case_builder import list_pending_cases

        if not list_pending_cases():
            return "（无待迭代案例，切片库为空或均已迭代）"
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
        variant = e.get("variant")
        if not isinstance(variant, dict) or variant.get("type") == "baseline":
            continue
        case_id = e.get("case_id", "")
        lines.append(
            f"- 案例 {case_id} r{e.get('round', '')}："
            f"改动 {variant.get('files', [])}，评分 {e.get('score', 0)}，"
            f"建议：{variant.get('instructions', '')}"
        )
        # 2026-08-14 用户反馈：正文信息不足，不知道 agent 改了什么——
        # 附上 patch 摘要（old_snippet → new_snippet 关键行）。
        # 注意：patch 是实验记录顶层字段（run_experiment_round 写入结构），
        # 不在 variant 内（variant 只含 type/files/instructions）。
        patch = e.get("patch")
        if isinstance(patch, dict):
            old = str(patch.get("old_snippet", "")).replace("\n", "⏎")[:80]
            new = str(patch.get("new_snippet", "")).replace("\n", "⏎")[:120]
            if old and new:
                lines.append(f"  - patch: `{old}` → `{new}`")
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
