"""事件报告持久化服务 — 将事件分析结果写入 Node.js /internal/analysis-reports

从 ``agents/workers/event.py`` 的 ``_persist_event_report`` 迁出。
"""

from datetime import datetime

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()


async def persist_event_report(
    event: str,
    display_report: dict[str, object] | None,
    podcast_brief: str,
) -> None:
    """持久化事件分析报告到 Node.js /internal/analysis-reports（非关键路径）。

    失败静默跳过，不影响主流程返回。
    """
    report_date = datetime.now().strftime("%Y-%m-%d")

    try:
        await node_api.post("/internal/analysis-reports", {
            "report_type": "event_conduction",
            "report_date": report_date,
            "user_id": "system",
            "content": {
                "event": event[:500],
                "display_report": display_report,
                "podcast_brief": podcast_brief,
            },
            "data_source": "event_agent_v2",
            "status": "completed",
        })
        logger.info("event_report_persisted", date=report_date)
    except Exception:
        logger.debug("event_report_persist_failed", exc_info=True)
