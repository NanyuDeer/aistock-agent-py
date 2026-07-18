"""事件报告持久化服务 — 将事件分析结果写入 Node.js /internal/analysis-reports

从 ``agents/workers/event.py`` 的 ``_persist_event_report`` 迁出。
"""

import copy
from datetime import datetime

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()

# 运行时状态字段：落库前必须从 analysis_reports 中剥离。
# 这些字段在落库请求发出前无法事先确定（event_persisted 此时必为 False），
# 写入会污染数据库使其自报未持久化。缓存可保存持久化状态，但数据库业务报告
# 与运行时状态应分离。
_RUNTIME_STATUS_FIELDS = ("event_generated", "event_persisted", "event_cached")


async def persist_event_report(
    event_id: str,
    event_meta: dict[str, object],
    event_text: str,
    analysis_reports: dict[str, object],
) -> bool:
    """持久化事件分析报告到 Node.js /internal/analysis-reports（非关键路径）。

    将完整事件元数据和完整 analysis_reports（四模块 + event_podcast_brief）写入
    Node.js，以 event_id 作为 user_id 列的隔离键，实现同日不同事件分别保存、
    同一事件重跑时 upsert 更新。

    落库前剥离运行时状态字段（event_generated/event_persisted/event_cached），
    避免数据库业务报告被无法事先确定的运行时状态污染。

    Returns:
        True 表示持久化成功，False 表示失败（调用方可据此上报 partial 状态）。
        检查 node_api.post() 返回值：None 或业务失败均返回 False，不记录成功日志。
    """
    report_date = datetime.now().strftime("%Y-%m-%d")

    # 深拷贝后剥离运行时状态字段：禁止原地修改调用方对象。
    persist_reports = copy.deepcopy(analysis_reports)
    for field in _RUNTIME_STATUS_FIELDS:
        persist_reports.pop(field, None)

    try:
        result = await node_api.post("/internal/analysis-reports", {
            "report_type": "event_conduction",
            "report_date": report_date,
            "event_id": event_id,
            "content": {
                "eventId": event_id,
                "title": event_meta.get("title", ""),
                "source": event_meta.get("source", ""),
                "publishTime": datetime.now().isoformat(),
                "event": event_text,
                "analysis_reports": persist_reports,
            },
            "data_source": "event_agent_v3",
            "status": "completed",
        })
        # 检查返回值：None 表示 HTTP 失败
        if result is None:
            logger.warning("event_report_persist_failed_none", event_id=event_id, date=report_date)
            return False
        logger.info("event_report_persisted", event_id=event_id, date=report_date)
        return True
    except Exception:
        logger.warning("event_report_persist_failed", event_id=event_id, exc_info=True)
        return False
