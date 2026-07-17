"""事件报告持久化服务 — 将事件分析结果写入 Node.js /internal/analysis-reports

从 ``agents/workers/event.py`` 的 ``_persist_event_report`` 迁出。
"""

from datetime import datetime

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()


async def persist_event_report(
    event_id: str,
    event_meta: dict[str, object],
    event_text: str,
    analysis_reports: dict[str, object],
) -> None:
    """持久化事件分析报告到 Node.js /internal/analysis-reports（非关键路径）。

    将完整事件元数据和完整 analysis_reports（四模块 + event_podcast_brief）写入
    Node.js，以 event_id 作为 user_id 列的隔离键，实现同日不同事件分别保存、
    同一事件重跑时 upsert 更新。

    失败静默跳过，不影响主流程返回。
    """
    report_date = datetime.now().strftime("%Y-%m-%d")

    try:
        await node_api.post("/internal/analysis-reports", {
            "report_type": "event_conduction",
            "report_date": report_date,
            "event_id": event_id,
            "content": {
                "eventId": event_id,
                "title": event_meta.get("title", ""),
                "source": event_meta.get("source", ""),
                "publishTime": datetime.now().isoformat(),
                "event": event_text,
                "analysis_reports": analysis_reports,
            },
            "data_source": "event_agent_v3",
            "status": "completed",
        })
        logger.info("event_report_persisted", event_id=event_id, date=report_date)
    except Exception:
        logger.debug("event_report_persist_failed", exc_info=True)
