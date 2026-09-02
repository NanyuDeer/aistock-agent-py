"""迭代完成邮件通知（2026-09-02）。

iterate 报告持久化后调用：经 app-api /internal/mail/notify 复用 EMAIL_SMTP_*（QQ 邮箱）
配置把"摘要+日期+类型"推送到收件邮箱（EMAIL_FROM）。自动（20:35 事件链路 / evening_chain）
与手动补跑统一在此收敛。任何失败仅告警，绝不阻断迭代链路。
"""

import json
from typing import Any

from structlog import get_logger

logger = get_logger()

# 迭代四维内部 key → 人读名称（对齐 iterate_analyzer 维度语义）
_DIM_LABELS = {
    "dimension_1": "关注点重叠度（命中率/新覆盖率）",
    "dimension_2": "方向-强度偏差",
    "dimension_3": "归因一致性（attribution_match_rate）",
    "dimension_4": "情绪基调偏差（sentiment_bias）",
}


def _to_text(value: object, max_len: int) -> str:
    if isinstance(value, str):
        text = value.strip()
    elif isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
    else:
        text = str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return text if len(text) <= max_len else text[: max_len - 1] + "…"


def format_iterate_text(payload: object) -> str | None:
    """从完整 iterate_payload 拼可读邮件正文（不用受控 brief_summary 的内部 key）。"""
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    triggered = payload.get("triggered_dimensions")
    trig: list[str] = triggered if isinstance(triggered, list) else []
    scorecard = payload.get("scorecard") if isinstance(payload.get("scorecard"), dict) else {}
    analysis = payload.get("analysis") if isinstance(payload.get("analysis"), dict) else {}

    if status == "normal" or not trig:
        return "今日迭代分析：无显著异常，四维指标均在阈值内。\n详情请前往 App 查看。"

    lines: list[str] = [f"状态：需关注（共 {len(trig)} 个维度触发阈值）"]
    for idx, dim in enumerate(trig, start=1):
        label = _DIM_LABELS.get(dim, dim)
        lines.append(f"\n{idx}. {label}")
        card = scorecard.get(dim)
        if isinstance(card, dict):
            metrics = card.get("metrics")
            if isinstance(metrics, dict) and metrics:
                lines.append(
                    "   指标：" + "，".join(f"{k}={v}" for k, v in metrics.items() if isinstance(v, (int, float)))
                )
        dim_analysis = analysis.get(dim)
        if dim_analysis is not None:
            lines.append("   分析：" + _to_text(dim_analysis, 240))

    suggestions = payload.get("optimization_suggestions")
    if isinstance(suggestions, list) and suggestions:
        lines.append("\n优化建议：")
        for sug in suggestions[:8]:
            if isinstance(sug, dict):
                dim = sug.get("dimension")
                label = _DIM_LABELS.get(str(dim)) if isinstance(dim, str) else ""
                text = sug.get("suggestion") or sug.get("text") or sug.get("evidence") or str(sug)
                lines.append(f"  - {('[' + label + '] ' if label else '')}{_to_text(text, 200)}")
            else:
                lines.append("  - " + _to_text(sug, 200))
    lines.append("\n详情请前往 App 查看。")
    return "\n".join(lines)


def _summary_text(summary: object) -> str:
    if summary is None:
        return ""
    if isinstance(summary, str):
        return summary
    return json.dumps(summary, ensure_ascii=False, default=str)


async def maybe_notify_iterate_mail(
    *,
    report_date: str,
    summary: object,
    report_type: str = "iterate",
    payload: Any | None = None,
) -> None:
    """静默推送通知邮件；node_api.post 内部吞 HTTP/业务错误返回 None，此处兜底异常。"""
    try:
        from aistock_agent.services.data_client import node_api

        body = format_iterate_text(payload) if isinstance(payload, dict) else None
        data = await node_api.post(
            "/internal/mail/notify",
            {
                "report_type": report_type,
                "report_date": report_date,
                "summary": body or _summary_text(summary),
            },
        )
        sent = bool(data and data.get("sent"))
        logger.info(
            "iterate_mail_notified",
            report_date=report_date,
            sent=sent,
            formatted=body is not None,
        )
    except Exception as exc:  # noqa: BLE001 — 邮件失败不阻断迭代
        logger.warning(
            "iterate_mail_notify_failed",
            report_date=report_date,
            error=str(exc),
            exc_info=True,
        )
