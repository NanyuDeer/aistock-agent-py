"""事件抓取数据源封装 — 统一事件抓取中台的采集层。

原则：复用现有 /internal/* 内部接口与已有 service，不重写爬虫。
采集层为确定性调用（非 LLM tool），由 event_scraper 条件边按 scrape_mode 编排。
"""

from __future__ import annotations

import asyncio
import json
import re as _re
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_scoring import apply_rule_score
from aistock_agent.services.event_store import EventRecord, normalize_event
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

_SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")


def _extract_items(resp: object, key: str = "items") -> list[Any]:
    """从 node_api 解包响应中安全提取列表字段（非 dict / 非 list → []）。

    node_api.get 返回 ``dict[str, object] | None``，逐层 isinstance 收窄，
    避免 mypy strict 的 object-not-iterable 与 None.get 报错。
    """
    if not isinstance(resp, dict):
        return []
    value = resp.get(key)
    if isinstance(value, list):
        return value
    return []


def _event_shanghai_date(published: str) -> str:
    """把 Node 返回的事件时间字符串转成上海时区日期（YYYY-MM-DD）。

    兼容三种格式：
    - UTC ISO 带 Z：'2026-08-12T02:00:00.000Z'（Node published_at TIMESTAMPTZ
      toISOString 输出，强制 UTC）→ 转上海时区再取日期
    - 带显式偏移但不以 Z 结尾：'2026-08-11T18:00:00+00:00' → 按原偏移换算
      上海墙钟（astimezone），不能 replace(tzinfo=) 覆盖原偏移
    - 本地无时区：'2026-08-12 10:00:00' / '2026-08-12T10:00:00'
      → 显式绑定上海时区（本机时区可能非上海，保证确定性）

    Why：Node 端 toISOString 输出 UTC，北京 00:00-07:59 的当日事件 UTC 日期
    落前一日（如 2026-08-11T22:00:00.000Z = 北京 8-12 06:00），用 UTC 日期前缀
    startswith(score_date) 比较会误过滤当日事件；必须按上海时区日期归属判断。
    带显式偏移的字符串同理：UTC 18:00 若被 replace(tzinfo=上海) 直接当成上海
    18:00，会错误落到前一日。解析失败时宽容回退取前 10 字符（原 startswith
    语义等价）。
    """
    raw = str(published).strip()
    if not raw:
        return ""
    try:
        if raw.endswith("Z"):
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return dt.astimezone(_SHANGHAI_TZ).strftime("%Y-%m-%d")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is not None:
            # 显式偏移（如 +00:00）：astimezone 按原偏移换算上海墙钟；
            # replace(tzinfo=上海) 会覆盖原偏移而不换算，导致日期归属错误
            dt = dt.astimezone(_SHANGHAI_TZ)
        else:
            # 本地无时区：显式绑定上海时区
            dt = dt.replace(tzinfo=_SHANGHAI_TZ)
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return raw[:10]


async def collect_cls_telegraph(score_date: str) -> list[EventRecord]:
    """财联社当日全量电报（按日全量）。失败降级 /internal/news/latest。

    Args:
        score_date: 交易日（YYYY-MM-DD）。

    Returns:
        归一化 EventRecord 列表（source=cls）。
    """
    try:
        resp = await node_api.get(
            f"/internal/news/telegraph?date={score_date}&limit=200"
        )
        items = _extract_items(resp)
        degraded = bool(resp.get("degraded")) if isinstance(resp, dict) else True
        if degraded or not items:
            raise RuntimeError(f"telegraph degraded or empty: {resp}")
    except Exception:  # noqa: BLE001
        logger.warning("cls_telegraph_failed_fallback_latest", date=score_date)
        resp = await node_api.get("/internal/news/latest")
        items = _extract_items(resp)

    events: list[EventRecord] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        raw: dict[str, Any] = dict(item)
        raw.setdefault("title", raw.get("content", ""))
        raw.setdefault("url", "")
        apply_rule_score(raw, source="cls")
        event = normalize_event(raw, source="cls", score_date=score_date)
        if event is not None:
            events.append(event)
    return events


async def collect_eastmoney_judgements(score_date: str) -> list[EventRecord]:
    """东方财富公告/新闻（复用已 AI 研判结果，不重复分析）。

    读取 stock_info_judgements 表（个股情报管线已闭环）。
    Node 端 StockMonitorService.getEvents 返回 ``{"total": N, "events": [...]}``
    （键名 events，非 items）；P0-2 修复后 alerts 接口支持 dateFrom 参数，
    按 ``published_at >= dateFrom``（上海 00:00）在 Node 端先过滤，Python 侧
    再按行 published_at/event_time 过滤，仅保留与 score_date 同日的行
    （按上海时区日期归属，兼容 Node UTC ISO 格式；避免昨日/前日陈旧行
    被标记为当日事件反复入库）。
    """
    try:
        # P0-2：Node /internal/monitor/alerts 原忽略 days 参数只取最新 20 行；
        # 改为显式 dateFrom 当日 00:00（上海）窗口，Node 端 published_at >= 过滤。
        resp = await node_api.get(
            f"/internal/monitor/alerts?dateFrom={score_date}T00:00:00%2B08:00"
        )
        rows = _extract_items(resp, key="events")
    except Exception as exc:  # noqa: BLE001
        logger.exception("eastmoney_judgements_failed", error=str(exc))
        return []

    events: list[EventRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw: dict[str, Any] = dict(row)
        # 当日窗口过滤（C1 修复后真实键名 events；I1 防跨日陈旧行误标当日）：
        # 时间格式可能是本地无时区（"2026-08-12 10:00:00" / "2026-08-12T10:00:00"）
        # 或 Node toISOString 输出的 UTC ISO（"2026-08-12T02:00:00.000Z"）。
        # 统一转成上海时区日期再与 score_date 比较——北京 00:00-07:59 当日事件
        # 的 UTC 日期落前一日（"2026-08-11T22:00:00.000Z" = 北京 8-12 06:00），
        # 旧 startswith(score_date) 前缀匹配会误过滤；无时间字段的行保守保留
        published = str(
            raw.get("event_time") or raw.get("published_at") or ""
        ).strip()
        if published and _event_shanghai_date(published) != score_date:
            continue
        # 对齐字段：Node mapJudgementToEvent 输出 detail_url（非 url/link），
        # normalize_event 只认 url/link → 这里补齐，否则东财事件 url 恒空
        # （I2：大盘溯源 causal_ready_count 不计入、stock_trace canonicalUrl 缺失）
        raw.setdefault("url", raw.get("detail_url") or "")
        # 对齐字段：ai_impact → direction 映射
        impact = str(raw.get("ai_impact", "")).strip()
        if "利好" in impact and "重大" in impact:
            raw["direction"] = "positive"
            raw["impact_score"] = 5
        elif "利空" in impact and "重大" in impact:
            raw["direction"] = "negative"
            raw["impact_score"] = 5
        elif "利好" in impact:
            raw["direction"] = "positive"
            raw["impact_score"] = 3
        elif "利空" in impact:
            raw["direction"] = "negative"
            raw["impact_score"] = 3
        else:
            raw["direction"] = "neutral"
            raw["impact_score"] = 1
        event = normalize_event(raw, source="eastmoney", score_date=score_date)
        if event is not None:
            events.append(event)
    return events


async def collect_ths_original(score_date: str) -> list[EventRecord]:
    """同花顺原创/涨停雷达（博主源，insight 模块已爬取）。

    读取 Node 新增接口 GET /internal/insight/sources?date=YYYY-MM-DD
    （查 watchlist_insight_sources 表按 trade_date 过滤，见 Step 3b Node 配合）。
    """
    try:
        resp = await node_api.get(f"/internal/insight/sources?date={score_date}")
        rows = _extract_items(resp)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ths_original_failed", error=str(exc))
        return []

    events: list[EventRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw: dict[str, Any] = dict(row)
        raw.setdefault("direction", "neutral")
        # 对齐字段：Node insight/sources 行字段为 source_url（非 url/link），
        # normalize_event 只认 url/link → 这里补齐，否则同花顺原创事件 url 恒空
        # （2026-09-24：与 eastmoney 的 detail_url 映射对齐，I2 同类问题）。
        raw.setdefault("url", raw.get("source_url") or raw.get("detail_url") or "")
        # Min-1：显式映射 summary/involved_keywords（Node 源字段为 content/keywords）。
        # keywords 是 JSONB，可能已解析为 list 或仍为 JSON 字符串，做防御解析。
        raw["summary"] = str(raw.get("content") or raw.get("summary") or "").strip()
        keywords = raw.get("keywords")
        if isinstance(keywords, list):
            raw["involved_keywords"] = [str(k) for k in keywords if isinstance(k, str)]
        elif isinstance(keywords, str):
            try:
                parsed = json.loads(keywords)
            except (TypeError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                raw["involved_keywords"] = [
                    str(k) for k in parsed if isinstance(k, str)
                ]
            else:
                raw["involved_keywords"] = []
        else:
            raw["involved_keywords"] = []
        apply_rule_score(raw, source="ths_original")
        event = normalize_event(raw, source="ths_original", score_date=score_date)
        if event is not None:
            events.append(event)
    return events


async def collect_tavily(score_date: str) -> list[EventRecord]:
    """Tavily 全网检索（复用大盘溯源查询词模板，参数化日期）。

    TavilyService.search 为同步阻塞调用，用 asyncio.to_thread 包装防阻塞事件循环
    （对齐 douyin_video 的「阻塞 IO 必须 to_thread」工程约束）。
    """
    from aistock_agent.services.tavily import TavilyService

    queries = [
        f"{score_date} 中国 资本市场 政策 产业 公告",
        f"{score_date} 全球股市 利率 汇率 大宗商品 地缘风险",
    ]
    events: list[EventRecord] = []
    for query in queries:
        try:
            result = await asyncio.to_thread(
                TavilyService().search, query, topic="news", max_results=5
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("tavily_search_failed", query=query, error=str(exc))
            continue
        # T4：溯源透传 — 读 result 的 provider 键（failover 命中 doubao/anysearch 时
        # 评分 apply_rule_score 不读 source，事件 source 保留真实命中源不破坏评分）。
        provider = str(result.get("provider", "tavily"))
        hits = _extract_items(result, key="results")
        for hit in hits:
            if not isinstance(hit, dict):
                continue
            raw = {
                "title": str(hit.get("title", "")),
                "summary": str(hit.get("content", "")),
                "url": str(hit.get("url", "")),
            }
            apply_rule_score(raw, source=provider)
            event = normalize_event(raw, source=provider, score_date=score_date)
            if event is not None:
                events.append(event)
    return events


async def collect_global_markets() -> list[EventRecord]:
    """外盘指数（隔夜美股/恒生/亚太），仅作为盘前档的行情事实事件。

    复用 market_tools.collect_global_market_facts（get_global_markets Tool 的
    结构化事实来源：[{ticker, name, price, change_pct, observed_at}]），
    不调 @tool 包装的字符串输出，保证 direction/impact_score 可计算。

    分级入库（用户裁决）：波动 >= 1% 记为重大事实（impact_score=5）过
    is_major_event 筛选落库；< 1% 记普通事实（impact_score=1），
    在 full_daily 分支被 is_major_event 过滤不落库。
    """
    from aistock_agent.tools.market_tools import collect_global_market_facts

    score_date = shanghai_today().isoformat()
    events: list[EventRecord] = []
    try:
        facts = await collect_global_market_facts(datetime.now(UTC))
        for fact in facts:
            name = str(fact.get("name") or fact.get("ticker") or "")
            # Min-5：无名称（name/ticker 均为空）的行情事实无标题可归一化，跳过
            if not name:
                continue
            price = fact.get("price")
            change_pct = fact.get("change_pct")
            pct = 0.0
            if isinstance(change_pct, int | float | str):
                try:
                    pct = float(change_pct)
                except (TypeError, ValueError):
                    pct = 0.0
            raw = {
                "title": f"{name} 隔夜表现",
                "summary": f"{name}: {price} ({pct}%)",
                "url": "",
                # 外盘行情快照无原文 URL 且无媒体名：显式携带来源名，
                # 经 normalize_event/传导透传，避免 LLM 判不出媒体恒显示"未知来源"
                # （2026-09-24）。
                "source_name": "外盘行情",
                "direction": (
                    "positive" if pct > 0 else "negative" if pct < 0 else "neutral"
                ),
                # Important 2：波动 >= 1% 记为重大事实（impact_score=5）过
                # is_major_event 落库；< 1% 为 1（full_daily 分支被过滤不落库）
                "impact_score": 5 if abs(pct) >= 1 else 1,
            }
            event = normalize_event(raw, source="global_markets", score_date=score_date)
            if event is not None:
                events.append(event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("global_markets_failed", error=str(exc))
    return events


_DATE_PATTERNS = (
    _re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})"),
    _re.compile(r"(\d{1,2})月(\d{1,2})日"),
)


def _parse_event_date(text: str, ref_year: int) -> str | None:
    for pat in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        g = m.groups()
        year = int(g[0]) if len(g) == 3 and len(g[0]) == 4 else ref_year
        month = int(g[0]) if len(g) == 2 else int(g[1])
        day = int(g[1]) if len(g) == 2 else int(g[2])
        try:
            # date 构造做日历合法性校验：13 月/2 月 30 日等非法日期
            # 自然抛 ValueError 落入下方 except（f-string 对已 int 值永不抛，是死代码）
            return date(year, month, day).isoformat()
        except ValueError:
            continue
    return None


# ---------- 重大事件时间线（P0.5，spec §5B.3 / §5A.3）：事件时间抽取 + 物化 ----------


_BARE_MD = _re.compile(r"(?<!\d)(\d{1,2})[-/月](\d{1,2})")


def _parse_event_date_flexible(frag: str, ref_year: int) -> str | None:
    """解析日期片段：年份形式（2026-09-17 / 2026年9月23日）优先；
    无年份 M/D（9/23）补 ref_year 复用 `_parse_event_date` 归一化。
    纯正则确定性实现，绝不引入相对日期/LLM 猜测（spec §5B.3）。
    """
    parsed = _parse_event_date(frag, ref_year)
    if parsed:
        return parsed
    return _parse_event_date(f"{ref_year}/{frag}", ref_year)


def _extract_event_start_time(title: str, content: str, ref_date: str) -> str | None:
    """抽取事件开始日期（spec §5B.3）：只认明确绝对日期，区间取开始日；无 → None。

    支持：年份形式绝对日期（2026-09-17 / 2026年9月23日）、无年份 M/D（9/23）、
    「X 至 Y」/「X-Y」/「X~Y」区间（只取开始日，spec §1.5 多日事件只在开始日落点）。
    相对日期（本周五/明日/下周）本阶段明确不支持（spec §5B.3 P2 才做）。
    """
    text = f"{title} {content}"
    ref_year = int(ref_date[:4])

    # 区间优先：`9-23 至 9-25` / `9/23-9/25` / `9/23~9/25`，取开始日
    range_match = _re.search(
        r"(?:20\d{2}[-/年])?\d{1,2}[-/月]\d{1,2}\s*(?:至|到|-|~)\s*"
        r"(?:20\d{2}[-/年])?\d{1,2}[-/月]\d{1,2}",
        text,
    )
    if range_match:
        start_m = _re.match(
            r"(?:20\d{2}[-/年])?\d{1,2}[-/月]\d{1,2}", range_match.group(0)
        )
        return _parse_event_date_flexible(start_m.group(0) if start_m else "", ref_year)

    # 无区间：整个文本抽绝对日期（年份形式优先）；无年份 M/D 兜底
    parsed = _parse_event_date(text, ref_year)
    if parsed:
        return parsed
    bare = _BARE_MD.search(text)
    if bare:
        return _parse_event_date_flexible(bare.group(0), ref_year)
    return None


def _normalize_datetime_to_iso(value: str) -> str | None:
    """把各类发布时间字符串归一到上海时区 ISO（publish_time_fallback 用）。

    支持：ISO（含 +08:00/Z）、'YYYY-MM-DD HH:mm[:ss]'、'YYYY-MM-DD'、unix 秒/毫秒。
    无时区/仅日期的输入一律按上海时区计（对齐 _event_shanghai_date 惯例）；
    解析失败返回 None（best-effort，绝不猜时间）。
    """
    text = str(value).strip()
    if not text:
        return None
    # unix 秒/毫秒（纯数字）
    if text.isdigit():
        ts = int(text)
        if ts > 10_000_000_000:  # 毫秒 → 秒
            ts //= 1000
        try:
            return datetime.fromtimestamp(ts, _SHANGHAI_TZ).isoformat(timespec="seconds")
        except (ValueError, OverflowError, OSError):
            return None
    # ISO 以 Z 结尾 → 补 +00:00（Python 3.10 fromisoformat 不支持 Z，统一兜底）
    normalized = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_SHANGHAI_TZ)
    return dt.astimezone(_SHANGHAI_TZ).isoformat(timespec="seconds")


def _extract_publish_time(event: EventRecord) -> str | None:
    """从事件原始数据抽取发布时间（spec §5B.3 publish_time_fallback）。

    字段优先级（覆盖各源通用名）：publish_time → published_at → event_time →
    ctime_stamp → ctime → create_time → date；payload 与 EventRecord 顶层都试
    （normalize_event 保留原始 raw 于 payload）。
    抽不出 → None（外层跳过物化——无法确定事件时间，不硬塞时间线）。
    """
    candidates: list[object] = []
    payload = event.get("payload")
    if isinstance(payload, dict):
        for key in (
            "publish_time",
            "published_at",
            "event_time",
            "ctime_stamp",
            "ctime",
            "create_time",
            "date",
        ):
            value = payload.get(key)
            if value is not None:
                candidates.append(value)
    for key in ("publish_time", "published_at"):
        value = event.get(key)
        if value is not None:
            candidates.append(value)
    for value in candidates:
        iso = _normalize_datetime_to_iso(str(value).strip())
        if iso:
            return iso
    return None


async def _materialize_event_entity(
    event: EventRecord, now_iso: str
) -> dict[str, str] | None:
    """事件物化到 /internal/event-entities（spec §5A.3/§5B.3 P0.5 收口）。

    「有明确绝对日期即物化」——未来/已发生都落（spec §5B.3 第 4 条，时间三分离：
    `event_start_time` 只认抽取的绝对日期）；抽不出日期 → 发布时间兜底
    （spec §5B.3 第 3 条 publish_time_fallback，`time_source` 区分），让全部重大
    新闻事件都能进时间线（2026-09-24 用户需求收口）；两者都没有 → 返回 None。
    `time_confidence`：news_extraction=0.9（正则命中绝对日期，确定性高）、
    publish_time_fallback=0.5（derived 低置信，前端可提示「以发布时间计」）；
    绝不 LLM 猜日期。
    端点未落地/失败 → warning、返回 None，绝不阻断抓取/传导主链路。
    返回 `{"event_id", "event_status"}` 供外层物化循环回填 EventRecord（A1a 裁决：
    本函数只物化不写回；None → 外层置未回填标记，守卫兜底走旧路径）。
    """
    if not settings.event_entity_enabled:
        return None
    # 事件范围收口（重大事件时间线）：普通个股事件不进时间线。
    # STOCK 事件在传导入口已被过滤（由个股情报 Agent 消费），时间线同样不收——
    # 否则「某公司高管变动/订单/业绩」等个股事件会挤占重大事件池。
    # 重大公司事件（重组/并购/技术突破等）的白名单放行属后续增强，本期一律不物化。
    if str(event.get("event_scope") or "").strip().upper() == "STOCK":
        logger.info(
            "event_entity_materialize_skipped_stock",
            title=str(event.get("title", ""))[:50],
            event_scope_source=str(event.get("event_scope_source", "")),
        )
        return None
    event_start = _extract_event_start_time(
        str(event.get("title", "")),
        str(event.get("summary", "")),
        str(event.get("score_date", now_iso[:10])),
    )
    if event_start:
        body: dict[str, object] = {
            "title": str(event.get("title", "")),
            "source_type": "news",
            "event_start_time": f"{event_start}T00:00:00+08:00",
            "time_source": "news_extraction",
            "time_confidence": 0.9,
        }
    else:
        # 抽不出绝对日期 → 发布时间兜底：事件时间 = 新闻发布时间（已发生事件按
        # 发布时间落时间线过去/今天；发布时间恒 ≤ now，不会误投为未来事件）。
        publish_iso = _extract_publish_time(event)
        if not publish_iso:
            return None
        body = {
            "title": str(event.get("title", "")),
            "source_type": "news",
            "event_start_time": publish_iso,
            "time_source": "publish_time_fallback",
            "time_confidence": 0.5,
        }
    try:
        resp = await node_api.post_event_entity(body)
        if isinstance(resp, dict) and resp.get("event_id"):
            logger.info(
                "event_entity_materialized",
                event_id=resp["event_id"],
                title=body["title"],
            )
            return {
                "event_id": str(resp["event_id"]),
                "event_status": str(resp.get("event_status") or ""),
            }
        logger.warning("event_entity_materialize_skipped", title=body["title"])
        return None
    except Exception:  # noqa: BLE001
        logger.warning("event_entity_materialize_failed", exc_info=True)
        return None
