"""case_scanner.py — 迭代切片的数据扫描器（最近交易日发现 + 电报事件扫描）。

review 切片只支持最近交易日：Node close-snapshot 无按日期查询接口，
find_recent_trading_day 取当日（15:30 后）或最近交易日（last-close 降级）。
event 切片扫描历史电报，按关键词 + 30 分钟窗口聚类为「重大事件」候选。
"""

from datetime import UTC, datetime, timedelta

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()

#: 事件发现关键词（首版启发式：强情绪词 + 政策词）
_EVENT_KEYWORDS: tuple[str, ...] = (
    "暴涨", "暴跌", "大涨", "大跌", "破纪录", "熔断",
    "降息", "加息", "降准", "证监会", "央行",
)

#: 同窗口合并阈值（分钟）
_CLUSTER_WINDOW_MINUTES = 30


def _normalize_date(value: object) -> str | None:
    """把 trade_date（YYYYMMDD 或 YYYY-MM-DD）归一化为 YYYY-MM-DD。"""
    if not isinstance(value, str):
        return None
    s = value.replace("-", "")
    if len(s) != 8 or not s.isdigit():
        return None
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}"


async def find_recent_trading_day() -> str | None:
    """返回最近一个已收盘交易日 YYYY-MM-DD；全部失败返回 None。

    优先当日 close-snapshot（15:30 后可用），降级 last-close-snapshot
    （严格早于今天的最近交易日）。
    """
    for path in ("/internal/market/close-snapshot", "/internal/market/last-close-snapshot"):
        try:
            data = await node_api.get(path)
        except Exception:  # noqa: BLE001 — 扫描器不允许单点失败阻断
            logger.warning("case_scanner_node_get_failed", path=path, exc_info=True)
            continue
        if data is not None:
            day = _normalize_date(data.get("trade_date"))
            if day is not None:
                return day
    return None


async def scan_major_events(days: int) -> list[dict[str, object]]:
    """扫描最近 days 天内电报，返回「重大事件」候选列表。

    每个候选：{"event_title", "event_time"(ISO), "telegraph_records"}。
    启发式：标题含 _EVENT_KEYWORDS 的电报为候选；同 30 分钟窗口合并；
    T = 聚类窗口末条电报时间；telegraph_records 为聚类窗口内所有电报
    （含未命中关键词的后续报道，供 agent 获取完整事件语料）。
    """
    today = datetime.now(UTC).date()
    candidates: list[dict[str, object]] = []
    for offset in range(days):
        day = (today - timedelta(days=offset)).isoformat()
        try:
            records = await node_api.get_list(f"/internal/news/telegraph?date={day}&limit=200")
        except Exception:  # noqa: BLE001
            logger.warning("case_scanner_telegraph_failed", date=day, exc_info=True)
            continue
        records = records or []
        candidates.extend(
            _cluster_events([r for r in records if isinstance(r, dict) and r.get("title")])
        )
    return candidates


def _cluster_events(records: list[dict[str, object]]) -> list[dict[str, object]]:
    """按关键词 + 30 分钟窗口把电报聚类为事件。"""
    hits = [r for r in records if _matches_keywords(str(r.get("title", "")))]
    hits.sort(key=lambda r: str(r.get("time", "")))
    clusters: list[list[dict[str, object]]] = []
    for record in hits:
        if clusters and _within_window(clusters[-1][-1], record):
            clusters[-1].append(record)
        else:
            clusters.append([record])

    events: list[dict[str, object]] = []
    for cluster in clusters:
        # T = 簇内最后一条记录的时间：簇成员链式合并后全部 time <= T，
        # 保证落盘时 build_case 的 time<=T 过滤不丢弃簇内后续报道（I2）。
        event_time = str(cluster[-1].get("time", ""))
        # 簇首条/末条无 time 字段时无法确定 T，跳过该簇（避免 fromisoformat("") 崩溃）
        if not event_time:
            continue
        events.append(
            {
                "event_title": str(cluster[0].get("title", "重大事件")),
                "event_time": event_time,
                # 整个簇：锚点起 30 分钟窗口内所有电报（含未命中关键词的后续报道）
                "telegraph_records": list(cluster),
            }
        )
    return events


def _matches_keywords(title: str) -> bool:
    return any(kw in title for kw in _EVENT_KEYWORDS)


def _within_window(first: dict[str, object], second: dict[str, object]) -> bool:
    t1 = _parse_time(first.get("time"))
    t2 = _parse_time(second.get("time"))
    if t1 is None or t2 is None:
        return False
    return 0 <= (t2 - t1).total_seconds() <= _CLUSTER_WINDOW_MINUTES * 60


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    except ValueError:
        return None
