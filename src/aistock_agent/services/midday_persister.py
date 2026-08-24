"""盘中报持久化服务 — 将盘中报结果写入 Node.js /internal/analysis-reports

与 morning_persister.py 同模式：非关键路径，失败返回 False 由调用方上报 partial 状态。
公共报告 ``user_id=None``，前端公开接口可读取。

H1（2026-08-24）：report_type 显式硬编码 "midday"，禁止复用/回落 morning，
否则会以盘中内容覆盖当日晨报（upsert 冲突键 report_type+report_date+COALESCE(user_id,'')）。

降级内容保护：persist 前校验降级，降级内容跳过持久化，避免污染公共库。
"""

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()


# 独立的盘中报降级判定（H5）：大盘默认 stocks 为空属预期，
# 不能沿用 morning 的 "stocks 与 risks 均空即降级" 规则（会误拒存大盘报）。
# 判定依据：details 去空白后过短（<50 字）视为降级内容。
def _is_degraded_report(report: dict[str, object]) -> bool:
    display = report.get("display_report")
    if not isinstance(display, dict):
        return True
    details = str(display.get("details", ""))
    stripped = "".join(details.split())
    # 空或过短 → 降级
    if len(stripped) < 50:
        return True
    schema_version = str(report.get("schema_version", ""))
    if "Sorry, need more steps" in details:
        return True
    if schema_version == "1.0" and len(stripped) < 100:
        return True
    return False


async def persist_midday_report(
    report: dict[str, object],
    report_date: str | None = None,
) -> bool:
    """持久化盘中报到 Node.js /internal/analysis-reports（非关键路径）。

    公共报告：``report_type="midday"``、``user_id=None``。

    降级内容保护：检测到降级报告时跳过持久化，返回 False。

    Args:
        report: 完整的双层报告结构 dict。
        report_date: 报告日期（YYYY-MM-DD），默认当天。

    Returns:
        True 表示持久化成功，False 表示失败或跳过（降级内容）。
    """
    if report_date is None:
        report_date = shanghai_today().isoformat()

    if _is_degraded_report(report):
        logger.warning("midday_report_persist_skipped_degraded", date=report_date)
        return False

    try:
        result = await node_api.post("/internal/analysis-reports", {
            "report_type": "midday",
            "report_date": report_date,
            "user_id": None,
            "content": report,
            "data_source": "midday_agent",
            "status": "completed",
        })
        if result is None:
            logger.warning("midday_report_persist_failed_none", date=report_date)
            return False
        logger.info("midday_report_persisted", date=report_date)
        return True
    except Exception:
        logger.warning("midday_report_persist_failed", date=report_date, exc_info=True)
        return False
