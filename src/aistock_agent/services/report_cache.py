"""报告内存缓存 — 替代 Node API 数据库持久化

agents/scheduler 触发后缓存生成的报告，
前端通过 /api/agent/reports 系列端点查询。
"""

from __future__ import annotations

import structlog

logger = structlog.get_logger()

# 缓存: key = "{report_type}:{report_date}" → dict
_cache: dict[str, dict[str, object]] = {}

REPORT_META: dict[str, dict[str, str]] = {
    "morning": {"label": "今日晨报", "icon": "sunrise"},
    "wind_leader": {"label": "风口龙头分析", "icon": "compass"},
    "hot_burst": {"label": "机构调研共振", "icon": "pulse"},
    "alert": {"label": "异动深度研判", "icon": "flash"},
    "broadcast": {"label": "双人财经播报", "icon": "podcast"},
    "review": {"label": "盘后复盘", "icon": "history"},
}


def set_report(report_type: str, report_date: str, content: object) -> None:
    """保存报告到内存缓存"""
    key = f"{report_type}:{report_date}"
    _cache[key] = {
        "report_type": report_type,
        "report_date": report_date,
        "content": content,
    }
    logger.info("report_cached", report_type=report_type, report_date=report_date)


def get_report(report_type: str, report_date: str) -> dict[str, object] | None:
    """获取单个报告"""
    return _cache.get(f"{report_type}:{report_date}")


def list_reports(report_date: str) -> list[dict[str, object]]:
    """列出指定日期的所有可用报告"""
    result: list[dict[str, object]] = []
    for report_type in ["morning", "wind_leader", "hot_burst", "broadcast", "alert", "review"]:
        r = _cache.get(f"{report_type}:{report_date}")
        if r:
            item = {
                "report_type": report_type,
                "report_date": r["report_date"],
                "label": REPORT_META.get(report_type, {}).get("label", report_type),
                "icon": REPORT_META.get(report_type, {}).get("icon", ""),
            }
            result.append(item)
    return result
