"""事件库读写服务 — 统一事件抓取中台的事件存储层。

事件统一以 ``report_type=event_scrape`` 写入 ``agent_analysis_reports`` 表
（JSONB content + COALESCE unique index），content_hash 作为幂等去重键。
本文件不重写任何数据源爬虫，只负责 EventRecord 归一化与落库/读取。
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any, TypedDict
from zoneinfo import ZoneInfo

import structlog

from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()

# 重大事件筛选阈值：仅保留 impact_score >= 4 的事件
MAJOR_IMPACT_THRESHOLD = 4


class EventRecord(TypedDict):
    """统一事件模型（收敛 stock_trace StockSourceRecord 与 review SourceRecord）。"""

    event_id: str
    title: str
    summary: str
    url: str
    impact_score: int
    direction: str
    involved_keywords: list[str]
    source: str
    source_level: str
    content_hash: str
    scrape_at: str
    score_date: str
    payload: dict[str, Any]


def event_content_hash(title: str, url: str) -> str:
    """生成事件去重键（sha1 of title+url）。"""
    return hashlib.sha1(f"{title}|{url}".encode()).hexdigest()


def _now_shanghai() -> str:
    """上海时钟当前时间字符串（2026-08-12 10:00:00）。

    显式用 Asia/Shanghai 时区（对齐 utils/date.py 惯例），避免依赖系统本地时区。
    """
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")


def normalize_event(
    raw: dict[str, Any],
    *,
    source: str,
    score_date: str,
) -> EventRecord | None:
    """将数据源原始条目归一化为 EventRecord。

    Args:
        raw: 数据源原始条目（财联社电报 item / stock_info_judgements 行等）。
        source: 数据源标识（cls/eastmoney/ths_original/tavily/global_markets）。
        score_date: 交易日归属（YYYY-MM-DD）。

    Returns:
        EventRecord；title 缺失时返回 None（该事件不可用）。
    """
    title = str(raw.get("title", "")).strip()
    if not title:
        return None

    summary = str(raw.get("summary") or raw.get("ai_summary") or "").strip()
    url = str(raw.get("url") or raw.get("link") or "").strip()
    if not url and source == "cls":
        # 仅财联社（cls）无 URL 时兜底详情页地址；eastmoney/ths 无 cls 详情页，
        # 不区分 source 会拼出错误链接
        news_id = str(raw.get("id", "")).strip()
        if news_id:
            url = f"https://www.cls.cn/detail/{news_id}"

    try:
        impact_score = int(raw.get("impact_score", 0))
    except (TypeError, ValueError):
        impact_score = 0

    direction = str(raw.get("direction", "neutral")).lower()
    if direction not in ("positive", "negative", "neutral"):
        direction = "neutral"

    keywords_raw = raw.get("involved_keywords") or raw.get("ai_keywords") or []
    involved_keywords = [str(k) for k in keywords_raw if isinstance(k, str)]

    source_level = str(raw.get("source_level", "C")).upper()
    if source_level not in ("A", "B", "C", "D"):
        source_level = "C"

    content_hash = event_content_hash(title, url)
    event_id = f"{score_date}-{content_hash[:16]}"

    return EventRecord(
        event_id=event_id,
        title=title,
        summary=summary,
        url=url,
        impact_score=impact_score,
        direction=direction,
        involved_keywords=involved_keywords,
        source=source,
        source_level=source_level,
        content_hash=content_hash,
        scrape_at=_now_shanghai(),
        score_date=score_date,
        payload=dict(raw),
    )


def is_major_event(record: EventRecord) -> bool:
    """重大事件判断：impact_score 达到阈值。"""
    return record["impact_score"] >= MAJOR_IMPACT_THRESHOLD


async def save_event_scrape(
    events: list[EventRecord],
    score_date: str,
) -> dict[str, Any]:
    """将归一化事件列表落库（report_type=event_scrape，当日幂等合并）。

    同日多次调用时先读当日已有事件，按 content_hash 合并后再落库，
    避免 Node 侧单行 upsert（(report_type, report_date, COALESCE(user_id,''))）
    整行覆盖 content 导致盘中增量丢失前批事件。

    Args:
        events: 归一化后的 EventRecord 列表。
        score_date: 交易日（YYYY-MM-DD）。

    Returns:
        {"persisted": int, "deduped": int, "error": str | None}
    """
    if not events:
        return {"persisted": 0, "deduped": 0, "error": None}

    existing = await load_event_scrape(score_date)
    existing_hashes = {e["content_hash"] for e in existing}
    merged = {e["content_hash"]: e for e in existing}
    merged.update({ev["content_hash"]: ev for ev in events})
    unique = list(merged.values())

    # 去重计数：本批中因重复被吸收的条数（同批内重复 + 与当日已有重复）
    seen = set(existing_hashes)
    deduped = 0
    for ev in events:
        if ev["content_hash"] in seen:
            deduped += 1
        else:
            seen.add(ev["content_hash"])

    try:
        result = await node_api.save_analysis_report(
            report_type="event_scrape",
            report_date=score_date,
            content={"events": unique, "schema_version": "1.0"},
            user_id=None,
            data_source="event_scraper",
            # 后台数据中台产物不进前端公共报告缓存（对齐 chat_analysis D15 先例）
            update_cache=False,
        )
        persisted = len(unique) if result is not None else 0
        return {"persisted": persisted, "deduped": deduped, "error": None}
    except Exception as exc:  # noqa: BLE001
        logger.exception("event_scrape_persist_failed", error=str(exc))
        return {"persisted": 0, "deduped": deduped, "error": str(exc)}


async def load_event_scrape(score_date: str) -> list[EventRecord]:
    """按日期读取当日抓取事件列表。

    读公共报告（user_id=None）：GET /internal/analysis-reports/event_scrape/{score_date}，
    node_api.get_analysis_report 已封装路径与解包。
    """
    try:
        report = await node_api.get_analysis_report("event_scrape", score_date)
        if report is None:
            return []
        content = report.get("content")
        if not isinstance(content, dict):
            return []
        events = content.get("events")
        if not isinstance(events, list):
            return []
        # 逐字段安全构造：不依赖 EventRecord(**ev)（TypedDict 动态键 mypy 报
        # typeddict-item），并对缺失/异常字段兜默认值，保证返回元素 schema 完整
        return [
            EventRecord(
                event_id=str(ev.get("event_id", "")),
                title=str(ev.get("title", "")),
                summary=str(ev.get("summary", "")),
                url=str(ev.get("url", "")),
                impact_score=int(ev.get("impact_score", 0) or 0),
                direction=str(ev.get("direction", "neutral")),
                involved_keywords=[
                    str(k) for k in ev.get("involved_keywords", []) if isinstance(k, str)
                ],
                source=str(ev.get("source", "")),
                source_level=str(ev.get("source_level", "C")),
                content_hash=str(ev.get("content_hash", "")),
                scrape_at=str(ev.get("scrape_at", "")),
                score_date=str(ev.get("score_date", "")),
                payload=ev.get("payload", {}) if isinstance(ev.get("payload", {}), dict) else {},
            )
            for ev in events
            if isinstance(ev, dict)
        ]
    except Exception as exc:  # noqa: BLE001
        logger.exception("event_scrape_load_failed", error=str(exc))
        return []


async def load_event_scrape_by_symbol(symbol: str, score_date: str) -> list[EventRecord]:
    """按标的读取当日抓取事件（stock_trace 证据源用）。"""
    events = await load_event_scrape(score_date)
    if not symbol:
        return events
    lowered = symbol.lower()
    return [
        ev
        for ev in events
        if lowered in str(ev.get("payload", {}).get("symbol", "")).lower()
        or any(lowered in str(k).lower() for k in ev.get("involved_keywords", []))
    ]
