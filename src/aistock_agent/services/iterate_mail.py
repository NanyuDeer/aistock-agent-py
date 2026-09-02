"""迭代完成邮件通知（2026-09-02）。

iterate 报告持久化后调用：经 app-api /internal/mail/notify 复用 EMAIL_SMTP_*（QQ 邮箱）
配置把"摘要+日期+类型"推送到收件邮箱（EMAIL_FROM）。自动（20:35 事件链路 / evening_chain）
与手动补跑统一在此收敛。任何失败仅告警，绝不阻断迭代链路。
"""

import json

from structlog import get_logger

logger = get_logger()


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
) -> None:
    """静默推送通知邮件；node_api.post 内部吞 HTTP/业务错误返回 None，此处兜底异常。"""
    try:
        from aistock_agent.services.data_client import node_api

        data = await node_api.post(
            "/internal/mail/notify",
            {
                "report_type": report_type,
                "report_date": report_date,
                "summary": _summary_text(summary),
            },
        )
        sent = bool(data and data.get("sent"))
        logger.info(
            "iterate_mail_notified",
            report_date=report_date,
            sent=sent,
        )
    except Exception as exc:  # noqa: BLE001 — 邮件失败不阻断迭代
        logger.warning(
            "iterate_mail_notify_failed",
            report_date=report_date,
            error=str(exc),
            exc_info=True,
        )
