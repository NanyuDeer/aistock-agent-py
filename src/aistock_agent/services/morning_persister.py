"""晨报报告持久化服务 — 将晨报结果写入 Node.js /internal/analysis-reports

与 ``event_persister.py`` 同模式：非关键路径，失败静默跳过。
公共报告 ``user_id=None``，前端公开接口可读取。
"""

from datetime import datetime

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()


async def persist_morning_report(
    report: dict[str, object],
    report_date: str | None = None,
) -> None:
    """持久化晨报到 Node.js /internal/analysis-reports（非关键路径）。

    公共报告：``report_type="morning"``、``user_id=None``。

    Args:
        report: 完整的双层报告结构 dict。
        report_date: 报告日期（YYYY-MM-DD），默认当天。
    """
    if report_date is None:
        report_date = datetime.now().strftime("%Y-%m-%d")

    try:
        await node_api.post("/internal/analysis-reports", {
            "report_type": "morning",
            "report_date": report_date,
            "user_id": None,
            "content": report,
            "data_source": "morning_agent",
            "status": "completed",
        })
        logger.info("morning_report_persisted", date=report_date)
    except Exception:
        logger.debug("morning_report_persist_failed", exc_info=True)
