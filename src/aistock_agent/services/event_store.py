"""事件库读写服务 — 统一事件抓取中台的事件存储层。

事件统一以 ``report_type=event_scrape`` 写入 ``agent_analysis_reports`` 表
（JSONB content + COALESCE unique index），content_hash 作为幂等去重键。
本文件不重写任何数据源爬虫，只负责 EventRecord 归一化与落库/读取。
"""

from __future__ import annotations

import asyncio
import hashlib
import unicodedata
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, NotRequired, TypedDict
from zoneinfo import ZoneInfo

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_origin import derive_event_origin
from aistock_agent.services.event_scope_resolver import resolve_event_scope

logger = structlog.get_logger()

# 重大事件筛选阈值：仅保留 impact_score >= 4 的事件
MAJOR_IMPACT_THRESHOLD = 4

# 落库读-改-写临界区锁（P0-4）：save_event_scrape 先读当日已有事件再合并
# 整行覆盖，load 与 save 之间跨 await，手动 trigger 与调度并发时后写覆盖
# 先写导致丢批。单进程内 asyncio.Lock 串行化整个临界区；多 worker 并发
# 属记录不裁决项（辩论裁决 D2），上多 worker 前需 DB 级并发控制。
_save_lock = asyncio.Lock()


class EventRecord(TypedDict):
    """统一事件模型（收敛 stock_trace StockSourceRecord 与 review SourceRecord）。

    `event_id`（score_date-content_hash[:16]）语义为 **source_event_id**（来源身份，
    spec §4.2）——不冒充 Event Entity 权威 id；权威 `app_event_id` 由 app-api 生成、
    上层挂接后写入（缺省 None，本模型仅声明不生成）。
    """

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
    # 股票关联字段：仅记录/后续个股情报联动，不参与个股事件判定
    symbol: str
    stock_name: str
    industry: str
    # 个股事件识别标记（第一阶段：STOCK/UNKNOWN 二值 + 规则来源 + 置信度）
    event_scope: str
    event_scope_source: str
    event_scope_confidence: float
    # 重大事件时间线（spec §4.2）：app-api 权威 event_id，由上层挂接写入（缺省 None）；
    # 本模型既有 `event_id` 语义为 **source_event_id**（来源身份），不冒充 Event Entity 权威 id。
    # TypedDict 可选键用 `str | None` 声明（未引入 typing_extensions），调用方一律 .get 消费。
    app_event_id: str | None
    # 重大事件时间线（spec §6.1/§6.2）：物化响应回填的 event_status（写库快照/读时重算，
    # app-api 权威）；缺省 None（未物化/开关关闭）时传导走旧路径（逐字节不变）。
    app_event_status: str | None
    # 数据源携带的来源名称（如"外盘行情"）：经传导 user_msg 透传，LLM 理解阶段
    # 可直接判定媒体名，避免外盘等无 URL 行情事件恒显示"未知来源"（2026-09-24）。
    source_name: str | None
    # 物化未回填（开关关闭/失败）时置位的兜底标记：随事件库 content 持久化，
    # 供后续排查；非落库契约必需键，故 NotRequired（构造时无需提供）。
    event_entity_unfilled: NotRequired[bool]
    # 事件地域标记（外围资讯，2026-10-15 批次）：单一真源 derive_event_origin；
    # 读侧有键读键、无键重算（load_event_scrape）。NotRequired 对齐 event_entity_unfilled 先例。
    event_origin: NotRequired[str]
    event_origin_source: NotRequired[str]
    event_origin_confidence: NotRequired[float]


def event_content_hash(title: str, url: str) -> str:
    """生成事件去重键（sha1 of title+url）。"""
    return hashlib.sha1(f"{title}|{url}".encode()).hexdigest()


# 近似去重标题相似度阈值：归一化标题 SequenceMatcher 比值超过该值视为近重复
# （对齐 forward_events._normalize_title 的轻归一口径，仅作标题语义近似判断）
TITLE_SIMILARITY_THRESHOLD = 0.9


def _normalize_title(s: str) -> str:
    """轻归一标题：去空白 + 去标点/符号 + 转小写（对齐 forward_events 同款实现）。

    仅用于近似去重（近重复标题判断），不做语义归并；两事件标题归一化后
    高度相似且同数据源时，视为同一事件的重复发稿（如财联社同日两条几乎
    相同的电报），吸收进已有事件而非新建。

    实现说明：Python 标准库 re 不支持 \\p{..} Unicode 属性转义，故用
    unicodedata.category 过滤等价实现（Punctuation/Symbol/空白）。
    """
    out: list[str] = []
    for ch in s.lower():
        if ch.isspace() or ch == "_":
            continue
        cat = unicodedata.category(ch)
        if cat.startswith("P") or cat.startswith("S"):
            continue
        out.append(ch)
    return "".join(out)


def _title_similarity(norm_a: str, norm_b: str) -> float:
    """两个归一化标题的相似度（SequenceMatcher ratio，0~1）。空串相似度为 0。"""
    if not norm_a or not norm_b:
        return 0.0
    return SequenceMatcher(None, norm_a, norm_b).ratio()


def _is_near_duplicate(ev: EventRecord, pool: list[EventRecord]) -> bool:
    """判断 ev 是否为 pool 中某事件的近重复（同数据源 + 归一化标题高相似）。

    仅限同数据源（source 相同），防止跨源相似标题误吸收（不同媒体对同一
    事件的独立报道应各自保留）。标题归一化后为空（无实质内容）不参与判断。
    """
    norm = _normalize_title(ev["title"])
    if not norm:
        return False
    for other in pool:
        if other["source"] != ev["source"]:
            continue
        other_norm = _normalize_title(other["title"])
        if not other_norm:
            continue
        if _title_similarity(norm, other_norm) > TITLE_SIMILARITY_THRESHOLD:
            return True
    return False


def _is_cross_source_absorbable(ev: EventRecord, other: dict[str, Any]) -> bool:
    """跨源近重复吸收：仅当『至少一侧 event_origin==peripheral』且标题相似度 >0.9。

    同源近重复由 _is_near_duplicate 负责（本函数对同源直接返回 False）。
    收紧条件（至少一侧 peripheral）避免破坏「独立媒体各自保留」既有口径。
    """
    if other.get("source") == ev["source"]:
        return False
    if not (
        ev.get("event_origin") == "peripheral" or other.get("event_origin") == "peripheral"
    ):
        return False
    return (
        _title_similarity(
            _normalize_title(ev["title"]),
            _normalize_title(str(other.get("title") or "")),
        )
        > TITLE_SIMILARITY_THRESHOLD
    )


def _normalize_symbol(value: str) -> str:
    """归一化股票代码：剥 SH/SZ/BJ 前缀，保留 6 位数字；非法返回空串。"""
    code = value.strip().upper()
    for prefix in ("SH", "SZ", "BJ"):
        if code.startswith(prefix):
            code = code[len(prefix) :]
            break
    return code if code.isdigit() else ""


def _safe_float(value: object, default: float = 0.0) -> float:
    """安全转 float，失败返回默认值（历史数据字段畸形不炸整批）。

    实现对齐同包 `global_importance_evaluation._safe_float`：先按 JSON 标量窄化
    （int/float 直转，其余走 str 兜底解析），消除 `float(object)` 在 mypy strict
    下的 arg-type 告警；数字字符串 / 可字符串化的数值对象（如 Decimal）仍可转，
    失败（None/畸形结构/非数值串）一律回落 default。契约由
    `test_safe_float_contract` 锁定（2026-10-02）。
    """
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (ValueError, TypeError):
        return default


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

    # 数据源携带的来源名称（如"外盘行情"）；无则 None，传导阶段回退 LLM 判定
    source_name = str(raw.get("source_name") or "").strip() or None

    content_hash = event_content_hash(title, url)
    event_id = f"{score_date}-{content_hash[:16]}"

    # 统一抽取股票关联字段（仅记录，不参与个股判定：symbol 只表示事件关联股票，
    # 不代表事件主体是单家公司，行业/产业链新闻可能携带关联 symbol）
    symbol = _normalize_symbol(str(raw.get("symbol") or raw.get("stock_code") or ""))
    stock_name = str(raw.get("stock_name", "")).strip()
    industry = str(raw.get("industry", "")).strip()

    # event_scope 单一裁决（STOCK > MARKET > UNKNOWN）+ event_origin 同 pass 计算
    scope, scope_source, scope_conf = resolve_event_scope(source, title, summary, raw)
    origin, origin_source, origin_conf = derive_event_origin(source, title, summary)

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
        symbol=symbol,
        stock_name=stock_name,
        industry=industry,
        source_name=source_name,
        event_scope=scope,
        event_scope_source=scope_source,
        event_scope_confidence=scope_conf,
        event_origin=origin,
        event_origin_source=origin_source,
        event_origin_confidence=origin_conf,
        # 来源侧无 app-api 权威 id/status：缺省 None（由上层物化/透传链路挂接）
        app_event_id=None,
        app_event_status=None,
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
        {"persisted": int, "deduped": int, "added": int,
         "added_events": list[EventRecord], "error": str | None}
        - persisted: 合并后库中事件总数（对外契约不变）。
        - deduped: 本批中因重复被吸收的条数（同批内重复 + 与当日已有重复）。
        - added: 本批真正新增去重后的事件数（不在当日已有 content_hash 集合），
          供 event_scraper 传导触发守卫（全去重批次 added=0 不重复触发传导）。
        - added_events: 本批新增子集（传导只对新增事件触发，降低 LLM 成本）。
    """
    if not events:
        return {
            "persisted": 0,
            "deduped": 0,
            "added": 0,
            "added_events": [],
            "error": None,
        }

    async with _save_lock:
        existing = await load_event_scrape(score_date)
        existing_hashes = {e["content_hash"] for e in existing}
        merged = {e["content_hash"]: e for e in existing}
        added_events: list[EventRecord] = []
        seen_added: set[str] = set()
        # 近似去重池：当日已有事件 + 本批已吸收新增（近似去重按源+归一化标题判断）
        near_pool = list(existing)
        # 被近似去重吸收的事件（content_hash 精确去重之外被吸收的近重复标题事件）
        near_absorbed: list[EventRecord] = []
        for ev in events:
            h = ev["content_hash"]
            if h in existing_hashes or h in seen_added:
                continue
            # 近似去重：同数据源 + 归一化标题高相似 → 吸收进已有事件，不触发传导
            # （财联社等数据源对同一事件可能发两条几乎相同的电报，见 2026-09-30 时间轴重复）
            same_source_near = _is_near_duplicate(ev, near_pool)
            # 跨源二次去重（S6）：仅当至少一侧 peripheral 且标题高度相似（防库膨胀 + 防传导双跑）
            cross_source_near = any(
                _is_cross_source_absorbable(ev, other) for other in near_pool
            )
            if same_source_near or cross_source_near:
                near_absorbed.append(ev)
                if cross_source_near:
                    logger.info(
                        "event_cross_source_absorbed",
                        event_id=str(ev.get("event_id", "")),
                        title=str(ev.get("title", ""))[:50],
                        source=str(ev.get("source", "")),
                    )
                continue
            seen_added.add(h)
            added_events.append(ev)
            merged[h] = ev
            near_pool.append(ev)
        unique = list(merged.values())

        # 去重计数：本批中因重复被吸收的条数（同批内重复 + 与当日已有重复）
        seen = set(existing_hashes)
        deduped = 0
        for ev in events:
            if ev["content_hash"] in seen:
                deduped += 1
            else:
                seen.add(ev["content_hash"])
        # 近似去重吸收的条数叠加进 deduped
        deduped += len(near_absorbed)

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
            return {
                "persisted": persisted,
                "deduped": deduped,
                "added": len(added_events),
                "added_events": added_events,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("event_scrape_persist_failed", error=str(exc))
            return {
                "persisted": 0,
                "deduped": deduped,
                "added": 0,
                "added_events": [],
                "error": str(exc),
            }


def _rebuild_event_record(ev: dict[str, Any]) -> EventRecord:
    """读时逐字段安全重建 EventRecord（含 event_origin 读时重算）。

    有键读键、无键 derive_event_origin 重算（存量行可被重判，不回落空串）。
    impact_score 畸形（非数值）抛 ValueError，由调用侧捕获跳过该条
    （保持「单条畸形只跳过该条」既有语义）。
    """
    impact_score = int(ev.get("impact_score", 0) or 0)
    origin = str(ev.get("event_origin") or "").strip() or derive_event_origin(
        str(ev.get("source") or ""), str(ev.get("title") or ""), str(ev.get("summary") or "")
    )[0]
    return EventRecord(
        event_id=str(ev.get("event_id", "")),
        title=str(ev.get("title", "")),
        summary=str(ev.get("summary", "")),
        url=str(ev.get("url", "")),
        impact_score=impact_score,
        direction=str(ev.get("direction", "neutral")),
        involved_keywords=[
            str(k) for k in ev.get("involved_keywords", []) if isinstance(k, str)
        ],
        source=str(ev.get("source", "")),
        source_level=str(ev.get("source_level", "C")),
        content_hash=str(ev.get("content_hash", "")),
        scrape_at=str(ev.get("scrape_at", "")),
        score_date=str(ev.get("score_date", "")),
        payload=(ev.get("payload", {}) if isinstance(ev.get("payload", {}), dict) else {}),
        symbol=str(ev.get("symbol", "")),
        stock_name=str(ev.get("stock_name", "")),
        industry=str(ev.get("industry", "")),
        event_scope=str(ev.get("event_scope", "UNKNOWN")),
        event_scope_source=str(ev.get("event_scope_source", "unknown")),
        event_scope_confidence=_safe_float(ev.get("event_scope_confidence"), 0.0),
        source_name=(str(ev["source_name"]) if ev.get("source_name") else None),
        app_event_id=(str(ev["app_event_id"]) if ev.get("app_event_id") else None),
        app_event_status=(str(ev["app_event_status"]) if ev.get("app_event_status") else None),
        event_origin=origin,
        event_origin_source=str(ev.get("event_origin_source") or "recomputed"),
        event_origin_confidence=_safe_float(ev.get("event_origin_confidence"), 0.0),
    )


async def load_event_scrape(score_date: str) -> list[EventRecord]:
    """按日期读取当日抓取事件列表。

    读公共报告（user_id=None）：GET /internal/analysis-reports/event_scrape/{score_date}。
    走 node_api.get_analysis_report_quiet（M2：空事件库是常态，404 降级为
    warning 而非 error 级日志——原有 get_analysis_report 经 _request 对 404
    打 error，每次空库读库都会刷 error 告警；不在 data_client 全局改，避免
    影响其他调用方）。
    """
    try:
        report = await node_api.get_analysis_report_quiet("event_scrape", score_date)
        if report is None:
            # 读不到报告（空事件库 404 或接口异常）：降级为 warning 级别
            logger.warning("event_scrape_report_not_found", date=score_date)
            return []
        content = report.get("content")
        if not isinstance(content, dict):
            return []
        events = content.get("events")
        if not isinstance(events, list):
            return []
        # 逐字段安全构造：不依赖 EventRecord(**ev)（TypedDict 动态键 mypy 报
        # typeddict-item），并对缺失/异常字段兜默认值，保证返回元素 schema 完整。
        # 单条事件字段畸形（如 impact_score 非数值）只跳过该条，不炸整批
        # （Task 1 Minor 1 顺手修：load 单字段畸形级联）。
        result: list[EventRecord] = []
        for ev in events:
            if not isinstance(ev, dict):
                continue
            # 逐字段安全构造 + event_origin 读时重算（见 _rebuild_event_record）；
            # 单条事件字段畸形（如 impact_score 非数值）只跳过该条，不炸整批。
            try:
                result.append(_rebuild_event_record(ev))
            except (TypeError, ValueError):
                logger.warning(
                    "event_scrape_load_skip_malformed",
                    event_id=str(ev.get("event_id", ""))[:32],
                )
                continue
        return result
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
