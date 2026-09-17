"""归因链组装与保存（spec P1a-3：大盘-板块-事件 链树的 agent 侧产物）。"""
import re

import structlog

from aistock_agent.agents.workers.sector_trace import judge_sector_driver_relation
from aistock_agent.services.data_client import node_api

logger = structlog.get_logger()

# 溯源未确认驱动原因时的 trace_summary 回退文案（区别于"溯源完成"占位——溯源
# insufficient 或无法从 stages 提取 trigger 结论时，如实说明原因未确认）。
_FALLBACK_TRACE_SUMMARY = "溯源未确认驱动原因"

# 弱依据日（无主链：板块提取走候选链/快照兜底）链根摘要回退文案：报告中
# attribution_summary 空缺时用中性表述，不编造主因（Task 9.1）。
_WEAK_ATTRIBUTION_SUMMARY = "证据不足，未确认主因"

# 大盘涨跌幅旧候选键：生产快照已不产出（真实形状是 a_share.indexes），
# 仅保留读取以兼容历史报告/旧 fixture。
_LEGACY_INDEX_PCT_KEYS = (
    "index_change_pct",
    "index_pct",
    "benchmark_change_pct",
    "sh_change_pct",
)

# 上证指数识别：code 取 000001（裸码）/ 000001.SH / SH000001 三种写法
_SHANGHAI_INDEX_CODES = frozenset({"000001", "000001.SH", "SH000001"})


def _numeric_pct(value: object) -> float | None:
    """仅接受真实数值（bool/字符串等视为缺失，不伪造 0）。"""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _index_items(indexes: object) -> list[dict[str, object]]:
    """兼容 a_share.indexes 两种形状，返回指数项列表。

    真实快照形状：normalize_a_share（market_trace_snapshot.py）把 Node 的 list
    归一化为 dict（key=SH000001 → 指数项）；list 形状为归一化前的原始载荷，
    两种都支持，避免下游按形状踩空。
    """
    if isinstance(indexes, dict):
        return [item for item in indexes.values() if isinstance(item, dict)]
    if isinstance(indexes, list):
        return [item for item in indexes if isinstance(item, dict)]
    return []


def _is_shanghai_index(item: dict[str, object]) -> bool:
    name = item.get("name")
    if isinstance(name, str) and "上证" in name:
        return True
    for key in ("ts_code", "code"):
        code = item.get(key)
        if isinstance(code, str) and code.upper() in _SHANGHAI_INDEX_CODES:
            return True
    return False


def index_pct_from_snapshot(snapshot: dict[str, object]) -> float | None:
    """从市场溯源快照解析大盘（上证）涨跌幅，缺失返回 None。

    真实快照形状为 ``a_share["indexes"]``：优先取上证指数项，找不到则取首项；
    值非数值视为缺失，再回退旧候选键（见 _LEGACY_INDEX_PCT_KEYS）。全部缺失
    返回 None——保持"未知"，不伪造 0（0 会让 relation 误判为 market_follow）。
    归因链（本模块）与板块溯源父链引用（event_consumers）共用，避免两处漂移。
    """
    if not isinstance(snapshot, dict):
        return None
    a_share = snapshot.get("a_share")
    if not isinstance(a_share, dict):
        return None
    items = _index_items(a_share.get("indexes"))
    chosen = next((item for item in items if _is_shanghai_index(item)), None)
    if chosen is None and items:
        chosen = items[0]
    if chosen is not None:
        for key in ("change_pct", "pct_chg"):  # pct_chg：Node 原始字段（归一化前）
            pct = _numeric_pct(chosen.get(key))
            if pct is not None:
                return pct
    for legacy_key in _LEGACY_INDEX_PCT_KEYS:
        pct = _numeric_pct(a_share.get(legacy_key))
        if pct is not None:
            return pct
    return None


def _trace_summary(trace_result: dict[str, object]) -> str:
    """从真实板块溯源 dump（SectorChainResult.model_dump(mode="json")）摘一句话。

    真实形状：{chain_id, sector, stages:[{kind, headline, claims, evidence}],
    attribution_status, missing_evidence}——没有 summary/observable_result 等
    顶层文案键。归因结论在 trigger stage（事件主因）的 headline/claims 里；
    attribution_status=insufficient 或无法提取（无 stages/无 trigger/无文本）
    时回退 _FALLBACK_TRACE_SUMMARY，避免显示"板块溯源完成"误导。
    """
    if not isinstance(trace_result, dict):
        return _FALLBACK_TRACE_SUMMARY
    if trace_result.get("attribution_status") == "insufficient":
        return _FALLBACK_TRACE_SUMMARY
    stages = trace_result.get("stages")
    if not isinstance(stages, list):
        return _FALLBACK_TRACE_SUMMARY
    trigger = next(
        (s for s in stages if isinstance(s, dict) and s.get("kind") == "trigger"),
        None,
    )
    if not isinstance(trigger, dict):
        return _FALLBACK_TRACE_SUMMARY
    headline = trigger.get("headline")
    if isinstance(headline, str) and headline.strip():
        return headline.strip()
    claims = trigger.get("claims")
    if isinstance(claims, list):
        for claim in claims:
            if isinstance(claim, str) and claim.strip():
                return claim.strip()
    return _FALLBACK_TRACE_SUMMARY


def _pct_from(snapshot: dict[str, object]) -> float | None:
    sector = snapshot.get("sector") if isinstance(snapshot, dict) else None
    if not isinstance(sector, dict):
        return None
    v = sector.get("pct_change")
    if v is None:
        # 兼容 wind-leaders 快照行（无 pct_change，只有 today_change 字段）
        v = sector.get("today_change")
    return float(v) if isinstance(v, int | float) else None


# --- R14：链板块标识增强（ts_code + 归一化权威名），消除前端按名匹配不上角色徽 ---

# 归一化口径**逐字对齐** app-api `ThsBoardService.normName` 与 app-frontend
# `utils/sectorInsight.normalizeSectorName`（去空白/括号 → 剥「（A股）/概念/板块/行业/产业链」
# 后缀 → 小写）。为什么必须同口径：前端用归一化名把链 child 桥到 THS 权威候选名，两侧
# 任一处口径漂移即回到"有链但角色徽不显示"（R14）。先删空白/括号再剥后缀的顺序也与前端一致
# （故「（A股）」两条分支在前端同样不可达，保留是为逐字对齐、便于比对）。
_SECTOR_STD_SPACE_RE = re.compile(r"[\s（）()]")
_SECTOR_STD_SUFFIX_RE = re.compile(r"（A股）|\(A股\)|概念$|板块$|行业$|产业链$")


def normalize_sector_std(name: object) -> str:
    """板块名归一化（R14）：非字符串/空归一化结果返回空串（调用方据此省略键）。"""
    if not isinstance(name, str):
        return ""
    return _SECTOR_STD_SUFFIX_RE.sub(
        "", _SECTOR_STD_SPACE_RE.sub("", name)
    ).lower()


def _sector_meta(sector: str, sector_row: object) -> dict[str, str]:
    """child 的板块标识增强字段（R14）：`ts_code` + `sector_std`（归一化权威名）。

    取不到即**省略键**（与仓库"无匹配省略键"惯例一致，不写 null）：
    - `ts_code`：仅取快照行（`extract_primary_sectors` 命中行，含 app-api SectorFact 的
      `ts_code`）的非空字符串，缺失/非字符串一律省略（不编造）；
    - `sector_std`：优先快照行 `name`（THS 权威榜名），行缺失/name 不可用时回退复盘原始
      `sector`（归一化仍是有效的桥接键，只是权威性较弱）；归一化结果为空则省略。
    """
    row = sector_row if isinstance(sector_row, dict) else {}
    meta: dict[str, str] = {}
    ts_code = row.get("ts_code")
    if isinstance(ts_code, str) and ts_code.strip():
        meta["ts_code"] = ts_code.strip()
    row_name = row.get("name")
    source_name = (
        row_name if isinstance(row_name, str) and row_name.strip() else sector
    )
    std = normalize_sector_std(source_name)
    if std:
        meta["sector_std"] = std
    return meta


# --- 链事件层（spec §3.2-4：中台优先 → 检索补漏，去重 + 上限，禁编造） ---

# 每板块事件节点上限（取最相关；超出丢弃并留痕）
MAX_CHAIN_EVENTS_PER_SECTOR = 3

_EVENT_SOURCE_WAREHOUSE = "warehouse"
_EVENT_SOURCE_SEARCH = "search"

# 板块定向检索来源的 kind 前缀（sector_trace_snapshot._normalize_source 产出
# kind=f"sector_event:{query}"）：链事件层的检索补漏**只消费定向检索产物**，
# 不另起检索（溯源快照已强制跑过，见 sector_trace_snapshot.build_sector_snapshot）。
_SECTOR_SOURCE_PREFIX = "sector_event:"

# 板块名常见后缀（"券商板块" 同时按 "券商" 匹配）；不建别名表——无权威别名源，
# 猜测性别名会引入误召回。
_SECTOR_NAME_SUFFIXES = ("板块", "概念", "行业", "指数")

# 盘面复述/表层转载特征词：命中降权（spec §3.2-4"追溯到最本质事件"——避免停在
# 复述当日行情/资金流的新闻上）。仅降权不排除；排除只按板块相关性门槛。
_RECAP_TITLE_MARKERS = ("收评", "午评", "早评", "复盘", "盘点", "资金流向", "涨停潮", "异动")


def _normalize_match_text(value: object) -> str:
    """匹配/去重归一化：仅保留字母数字与汉字（去空格、标点、大小写差异）。"""
    if not isinstance(value, str):
        return ""
    return "".join(ch for ch in value.lower() if ch.isalnum())


def _sector_tokens(sector: str) -> list[str]:
    """板块匹配词：板块名 + 剥常见后缀后的核心名（长度 ≥2 才用）。"""
    name = (sector or "").strip()
    if not name:
        return []
    tokens = [name]
    for suffix in _SECTOR_NAME_SUFFIXES:
        if name.endswith(suffix) and len(name) - len(suffix) >= 2:
            tokens.append(name[: -len(suffix)])
    return tokens


def _mentions(text: str, tokens: list[str]) -> bool:
    return any(token in text for token in tokens if token)


def _warehouse_match_weight(tokens: list[str], event: dict[str, object]) -> int:
    """中台事件与板块的相关度权重（确定性：实体 > 关键词 > 标题 > 摘要；0 = 不相关）。"""
    industry = str(event.get("industry") or "")
    if industry and _mentions(industry, tokens):
        return 3
    raw_keywords = event.get("involved_keywords")
    keywords = (
        [str(k) for k in raw_keywords if isinstance(k, str)]
        if isinstance(raw_keywords, list)
        else []
    )
    for token in tokens:
        if any(len(k) >= 2 and (token in k or k in token) for k in keywords):
            return 3
    if _mentions(str(event.get("title") or ""), tokens):
        return 2
    if _mentions(str(event.get("summary") or ""), tokens):
        return 1
    return 0


def _node(
    *,
    event_id: str | None,
    ref: str,
    headline: str,
    source: str,
    content_hash: str = "",
) -> dict[str, object]:
    """事件节点（内部形状：公开四字段 + 去重键；_dedup 后投影为公开契约）。"""
    return {
        "event_id": event_id,
        "ref": ref,
        "headline": headline,
        "source": source,
        "_title_key": _normalize_match_text(headline),
        "_content_hash": content_hash,
        "_url_key": ref if ref.startswith(("http://", "https://")) else "",
    }


def _warehouse_candidates(
    sector: str, warehouse_events: list[dict[str, object]]
) -> list[dict[str, object]]:
    """中台存量命中（spec §3.2-4 ①）：按板块别名/关键词/实体匹配，权重→影响力→原序。"""
    tokens = _sector_tokens(sector)
    if not tokens:
        return []
    scored: list[tuple[int, int, int, dict[str, object]]] = []
    for index, event in enumerate(warehouse_events):
        if not isinstance(event, dict):
            continue
        weight = _warehouse_match_weight(tokens, event)
        if weight <= 0:
            continue
        title = str(event.get("title") or "").strip()
        # 契约：source=warehouse 的 event_id 必须非空；无标题无法作为事件摘要 →
        # 二者任一缺失即不产节点（宁缺不造，不用 url 冒充 id）
        event_id = str(event.get("event_id") or event.get("app_event_id") or "").strip()
        if not title or not event_id:
            continue
        url = str(event.get("url") or "").strip()
        impact = event.get("impact_score")
        impact_score = impact if isinstance(impact, int) else 0
        scored.append(
            (
                -weight,
                -impact_score,
                index,
                _node(
                    event_id=event_id,
                    ref=url or f"event:{event_id}",
                    headline=title,
                    source=_EVENT_SOURCE_WAREHOUSE,
                    content_hash=str(event.get("content_hash") or ""),
                ),
            )
        )
    scored.sort(key=lambda item: (item[0], item[1], item[2]))
    return [item[3] for item in scored]


def _trigger_evidence_urls(trace_result: dict[str, object]) -> set[str]:
    """溯源 trigger 阶段引用的来源 URL（最贴近根因的判断来自既有溯源产物，非新增 LLM 判定）。"""
    stages = trace_result.get("stages") if isinstance(trace_result, dict) else None
    if not isinstance(stages, list):
        return set()
    urls: set[str] = set()
    for stage in stages:
        if not isinstance(stage, dict) or stage.get("kind") != "trigger":
            continue
        evidence = stage.get("evidence")
        if not isinstance(evidence, list):
            continue
        for ref in evidence:
            url = str(ref.get("url") or "").strip() if isinstance(ref, dict) else ""
            if url:
                urls.add(url)
    return urls


def _search_candidates(
    sector: str,
    trace_result: dict[str, object],
    snapshot: dict[str, object],
) -> list[dict[str, object]]:
    """检索补漏（spec §3.2-4 ②，溯源板块一律强制执行）。

    消费溯源快照的定向检索来源（sources[].kind="sector_event:<query>"，由
    sector_trace_snapshot._run_directed_searches 真实产出）。相关性门槛：标题/正文
    命中板块词，或 URL 被 trigger 阶段引用；门槛不过 → 不产节点（不编造事件）。
    """
    sources = snapshot.get("sources") if isinstance(snapshot, dict) else None
    if not isinstance(sources, list):
        return []
    tokens = _sector_tokens(sector)
    evidence_urls = _trigger_evidence_urls(trace_result)
    scored: list[tuple[int, int, int, int, int, dict[str, object]]] = []
    for index, item in enumerate(sources):
        if not isinstance(item, dict):
            continue
        kind = str(item.get("kind") or "")
        if not kind.startswith(_SECTOR_SOURCE_PREFIX):
            continue
        query = kind[len(_SECTOR_SOURCE_PREFIX) :]
        title = str(item.get("title") or "").strip()
        content = str(item.get("content") or "")
        headline = title or content[:60].strip()
        if not headline:
            continue
        url = str(item.get("url") or "").strip()
        in_title = _mentions(title, tokens)
        in_content = _mentions(content, tokens)
        by_evidence = bool(url) and url in evidence_urls
        if not (by_evidence or in_title or in_content):
            continue
        recap = 1 if any(marker in title for marker in _RECAP_TITLE_MARKERS) else 0
        scored.append(
            (
                0 if by_evidence else 1,
                0 if in_title else 1,
                0 if in_content else 1,
                recap,
                index,
                _node(
                    event_id=None,  # 检索来源无中台权威 id（不冒充）
                    ref=url or f"search:{query}|{headline}",
                    headline=headline,
                    source=_EVENT_SOURCE_SEARCH,
                ),
            )
        )
    scored.sort(key=lambda item: (item[0], item[1], item[2], item[3], item[4]))
    return [item[5] for item in scored]


def _is_same_event(left: dict[str, object], right: dict[str, object]) -> bool:
    """同一现实事件判定（确定性：content_hash → URL → 标题归一化/互相包含）。"""
    left_hash, right_hash = str(left["_content_hash"]), str(right["_content_hash"])
    if left_hash and right_hash and left_hash == right_hash:
        return True
    if left["_url_key"] and left["_url_key"] == right["_url_key"]:
        return True
    left_title, right_title = str(left["_title_key"]), str(right["_title_key"])
    if not left_title or not right_title:
        return False
    if left_title == right_title:
        return True
    shorter, longer = sorted((left_title, right_title), key=len)
    return len(shorter) >= 8 and shorter in longer


def _child_events(
    sector: str,
    trace_result: dict[str, object],
    snapshot: dict[str, object],
    warehouse_events: list[dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, int]]:
    """板块事件节点集合：中台优先 → 检索补漏 → 去重 → 上限（返回节点 + 留痕计数）。"""
    candidates = _warehouse_candidates(sector, warehouse_events)
    candidates.extend(_search_candidates(sector, trace_result, snapshot))
    kept: list[dict[str, object]] = []
    dropped = 0
    for candidate in candidates:
        if any(_is_same_event(candidate, existing) for existing in kept):
            dropped += 1
            continue
        kept.append(candidate)
    capped = len(kept) - MAX_CHAIN_EVENTS_PER_SECTOR
    kept = kept[:MAX_CHAIN_EVENTS_PER_SECTOR]
    events = [
        {
            "event_id": item["event_id"],
            "ref": item["ref"],
            "headline": item["headline"],
            "source": item["source"],
        }
        for item in kept
    ]
    stats = {
        "warehouse": sum(1 for e in events if e["source"] == _EVENT_SOURCE_WAREHOUSE),
        "search": sum(1 for e in events if e["source"] == _EVENT_SOURCE_SEARCH),
        "deduped": dropped,
        "capped": max(capped, 0),
    }
    return events, stats


async def load_chain_warehouse_events(report_date: str) -> list[dict[str, object]]:
    """读当日中台存量事件（事件抓取中台 report_type=event_scrape）供链事件层匹配。

    复用 event_store.load_event_scrape（内部已吞异常返回 []，空事件库是常态）：
    链事件层不因中台不可用而失败——events 退化为检索补漏或空数组。
    """
    from aistock_agent.services.event_store import load_event_scrape  # noqa: PLC0415

    events = await load_event_scrape(report_date)
    return [dict(event) for event in events if isinstance(event, dict)]


def _attribution_parent(result: object) -> dict[str, object]:
    """板块溯源结果携带的报告 attribution_parent（sector_trace.py 写入的报告字段）。

    run_sector_trace 把要落库的 content["attribution_parent"] 原样带入结果：板块
    溯源报告与链路同键 report_date（多板块同日互相覆盖），回读无法区分板块，故由
    写入侧携带、链组装消费（Task 2.2 修"只写不读"）。
    """
    parent = getattr(result, "attribution_parent", None)
    return parent if isinstance(parent, dict) else {}


def _sector_extraction(result: object) -> dict[str, object]:
    """板块溯源结果携带的提取来源/弱标记（SectorTraceRunResult.extraction）。

    SectorTraceConsumer 消费 extract_primary_sectors 的 SectorHit 后写入
    ``{"source": "primary_claim"|"candidate_claim"|"snapshot", "weak": bool}``；
    缺该属性（旧调用方/回放）→ 空 dict，视同主链命中（不误标弱）。
    """
    value = getattr(result, "extraction", None)
    return value if isinstance(value, dict) else {}


def _reconcile_index_pct(
    report_date: str, snapshot_index_pct: float | None, sector_results: list[object]
) -> float | None:
    """用报告 attribution_parent.index_pct 校验/补全大盘涨跌幅（以报告为准并告警）。

    - 报告缺该字段 → 沿用现有组装逻辑（快照口径，向后兼容）；
    - 快照缺失、报告有 → 补全（info）；
    - 两者不一致 → warning chain_parent_mismatch 并采用报告值（不静默）；
    - 多板块报告之间不一致 → warning（以首个为准，便于排查父链引用漂移）。
    """
    authoritative: float | None = None
    for result in sector_results:
        parent = _attribution_parent(result)
        value = _numeric_pct(parent.get("index_pct")) if parent else None
        if value is None:
            continue
        if authoritative is None:
            authoritative = value
            continue
        if value != authoritative:
            logger.warning(
                "chain_parent_mismatch",
                report_date=report_date,
                field="index_pct",
                sector=str(getattr(result, "sector", "") or ""),
                report_index_pct=value,
                first_report_index_pct=authoritative,
            )
    if authoritative is None:
        return snapshot_index_pct
    if snapshot_index_pct is None:
        logger.info(
            "chain_parent_index_filled",
            report_date=report_date,
            report_index_pct=authoritative,
        )
    elif snapshot_index_pct != authoritative:
        logger.warning(
            "chain_parent_mismatch",
            report_date=report_date,
            field="index_pct",
            source="snapshot_vs_report",
            snapshot_index_pct=snapshot_index_pct,
            report_index_pct=authoritative,
        )
    return authoritative


def assemble_attribution_chain(
    report_date: str,
    review_payload: dict[str, object],
    sector_results: list[object],
    warehouse_events: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    """组装 大盘(market) → 主驱动板块(self_driven/follow) 归因链。

    `warehouse_events` 为当日中台存量事件（`load_chain_warehouse_events` 产物，供
    children[].events 的"中台优先"匹配）；缺省 None = 不做中台匹配，只走检索补漏。

    Task 9.1：sector_results 携带的 extraction（板块提取来源/弱标记，见
    SectorTraceRunResult.extraction）为弱依据时 → children[] 写 extraction 标弱、
    root 写 evidence_weak（+报告 attribution_status，摘要空缺用中性表述，不编造主因）；
    主链命中路径不写这些键（正常链不被弱标记污染）。

    R14：children[] 加性写 `ts_code`/`sector_std`（快照权威行，取不到省略键，见
    `_sector_meta`）——供前端把链板块桥到 THS 权威候选（消除命名漂移导致的角色徽丢失）；
    `sector` 保持复盘原始名不变（app-api 校验要求非空字符串，向后兼容）。
    """
    report = review_payload.get("report")
    content = report.get("content") if isinstance(report, dict) else None
    content = content if isinstance(content, dict) else None
    mt = content.get("market_trace") if isinstance(content, dict) else None
    mt = mt if isinstance(mt, dict) else None
    snapshot = mt.get("snapshot") if isinstance(mt, dict) else None
    trace = mt.get("trace") if isinstance(mt, dict) else None

    # 大盘涨跌幅来自快照 a_share.indexes（旧四个候选键在生产快照并不存在，
    # 曾导致 root.index_pct 恒 None → children relation 恒 unknown）
    snapshot_index_pct = (
        index_pct_from_snapshot(snapshot) if isinstance(snapshot, dict) else None
    )
    # Task 2.2：消费板块溯源报告写入的 attribution_parent（原先只写不读）——
    # 校验/补全大盘涨跌幅，不一致以报告为准并 warning
    index_pct = _reconcile_index_pct(report_date, snapshot_index_pct, sector_results)

    summary = str(trace.get("attribution_summary") or "") if isinstance(trace, dict) else ""

    children: list[dict[str, object]] = []
    for res in sector_results:
        sector = str(getattr(res, "sector", "") or "")
        trace_result = getattr(res, "trace_result", {}) or {}
        snapshot_dict = getattr(res, "snapshot", {}) or {}
        pct = _pct_from(snapshot_dict) if isinstance(snapshot_dict, dict) else None
        # 链事件层（spec §3.2-4）：中台优先 → 检索补漏 → 去重 → 上限；无命中为空数组
        events, event_stats = _child_events(
            sector,
            trace_result if isinstance(trace_result, dict) else {},
            snapshot_dict if isinstance(snapshot_dict, dict) else {},
            warehouse_events or [],
        )
        if events or event_stats["deduped"] or event_stats["capped"]:
            # 判定留痕（spec §3.2-4 去重/上限口径调参用）
            logger.info(
                "chain_sector_events",
                report_date=report_date,
                sector=sector,
                **event_stats,
            )
        extraction = _sector_extraction(res)
        child: dict[str, object] = {
            "sector": sector,
            # R14：板块标识增强（ts_code/sector_std，取自溯源命中的快照权威行）——
            # sector 保持复盘原始名（app-api 校验要求非空字符串，向后兼容）
            **_sector_meta(sector, getattr(res, "sector_row", None)),
            "relation": judge_sector_driver_relation(pct, index_pct),
            "pct": pct,
            "trace_summary": _trace_summary(trace_result),
            "events": events,
        }
        # Task 9.1：兜底命中（无主链 → 候选链/快照）标弱依据，供展示层提示证据不足；
        # 主链命中不写该键（正常路径不被弱标记污染）
        if extraction.get("weak"):
            child["extraction"] = {
                "source": str(extraction.get("source") or ""),
                "weak": True,
            }
        children.append(child)

    # 整体走 T2/T3（无主链）→ 链根如实标注证据弱；摘要空缺用中性表述，不编造主因
    evidence_weak = any(_sector_extraction(res).get("weak") for res in sector_results)
    root: dict[str, object] = {
        "type": "market",
        "date": report_date,
        "summary": summary,
        "index_pct": index_pct,
    }
    if evidence_weak:
        status = str(trace.get("attribution_status") or "") if isinstance(trace, dict) else ""
        if status:
            root["attribution_status"] = status
        root["evidence_weak"] = True
        if not summary:
            root["summary"] = _WEAK_ATTRIBUTION_SUMMARY

    return {
        "date": report_date,
        "root": root,
        "children": children,
    }


class AttributionChainStore:
    def __init__(self) -> None:
        self.node_api = node_api

    async def save(self, report_date: str, chain: dict[str, object]) -> None:
        # 路径必须带 /api 前缀：app-api 把 attributionChainRouter 挂在 /api 下
        # （index.ts:165），绝对路径为 POST /api/internal/attribution-chain；不带 /api 会命中
        # /internal 那个 router（index.ts:631）而恒 404，且 post 吞错返回 None → 静默不落库。
        result = await self.node_api.post(
            "/api/internal/attribution-chain", {"date": report_date, "chain": chain}
        )
        if result is None:
            # data_client.post 失败/业务码异常吞错返回 None → 告警而非误报 saved
            logger.warning(
                "attribution_chain.save_failed",
                report_date=report_date,
                error="node_api.post 返回 None（请求失败或业务码异常）",
            )
            return
        logger.info(
            "attribution_chain.saved",
            report_date=report_date,
            children=len(chain.get("children", [])),
        )
