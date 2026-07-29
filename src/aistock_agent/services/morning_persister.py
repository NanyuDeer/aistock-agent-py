"""晨报报告持久化服务 — 将晨报结果写入 Node.js /internal/analysis-reports

与 ``event_persister.py`` 同模式：非关键路径，失败返回 False 由调用方上报 partial 状态。
公共报告 ``user_id=None``，前端公开接口可读取。

降级内容保护：persist 前调用 ``_is_degraded_report`` 校验，
若为 LLM 解析失败的降级文本则跳过持久化，避免污染数据库导致 brief_morning 聚合异常。
"""

from datetime import datetime

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()


async def persist_morning_report(
    report: dict[str, object],
    report_date: str | None = None,
) -> bool:
    """持久化晨报到 Node.js /internal/analysis-reports（非关键路径）。

    公共报告：``report_type="morning"``、``user_id=None``。

    降级内容保护：检测到降级报告时跳过持久化，返回 False。
    校验与 morning.run() 缓存写入前校验一致，双重防护避免降级内容入库。

    Args:
        report: 完整的双层报告结构 dict。
        report_date: 报告日期（YYYY-MM-DD），默认当天。

    Returns:
        True 表示持久化成功，False 表示失败或跳过（降级内容）。
        检查 node_api.post() 返回值：None 或业务失败均返回 False，不记录成功日志。
    """
    if report_date is None:
        report_date = datetime.now().strftime("%Y-%m-%d")

    # 降级内容保护：不污染数据库（延迟导入避免与 morning.py 的持久化导入循环引用）
    from aistock_agent.agents.workers.morning import _is_degraded_report

    if _is_degraded_report(report):
        logger.warning("morning_report_persist_skipped_degraded", date=report_date)
        return False

    try:
        result = await node_api.post("/internal/analysis-reports", {
            "report_type": "morning",
            "report_date": report_date,
            "user_id": None,
            "content": report,
            "data_source": "morning_agent",
            "status": "completed",
        })
        # 检查返回值：None 表示 HTTP 失败
        if result is None:
            logger.warning("morning_report_persist_failed_none", date=report_date)
            return False
        logger.info("morning_report_persisted", date=report_date)
        return True
    except Exception:
        logger.warning("morning_report_persist_failed", date=report_date, exc_info=True)
        return False
