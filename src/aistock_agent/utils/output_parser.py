"""输出解析器 — LLM 输出解析工具集

- ``parse_event_output``：事件 Agent 双层输出解析（display_report + podcast_brief）
- ``extract_major_events``：晨报 Agent 重大事件提取（从标记块或 JSON 数组）
"""

import json
import re
from collections.abc import Sequence
from typing import cast

import structlog
from langchain_core.messages import BaseMessage

from aistock_agent.utils.message import extract_final_ai_response

logger = structlog.get_logger()


def parse_event_output(
    messages: Sequence[BaseMessage | dict[str, str]],
) -> tuple[dict[str, object] | None, str | None]:
    """从 LLM 消息列表解析 display_report + podcast_brief。

    解析策略（逐级回退）：
    1. 提取最后一条 AI 消息，尝试整段 JSON 解析
    2. 如果失败，正则匹配 JSON 块（花括号平衡）
    3. 再失败则返回 (None, None)

    Returns:
        (display_report, podcast_brief) 元组，解析失败均返回 None。
    """
    text = extract_final_ai_response(messages)
    if not text:
        logger.warning("event_output_parse_empty_text")
        return (None, None)

    # 策略 1: 整段 JSON 解析
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return _extract_fields(parsed)
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略 2: 正则匹配 JSON 块（花括号平衡）
    match = re.search(r'\{.*\}', text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return _extract_fields(parsed)
        except (json.JSONDecodeError, TypeError):
            pass

    logger.warning("event_output_parse_failed", text_preview=text[:200])
    return (None, None)


def _extract_fields(parsed: dict[str, object]) -> tuple[dict[str, object] | None, str | None]:
    """从解析后的 dict 提取 display_report 和 podcast_brief"""
    display = parsed.get("display_report")
    brief = parsed.get("podcast_brief")

    display_dict = display if isinstance(display, dict) else None
    brief_str = brief if isinstance(brief, str) else (str(brief) if brief else None)

    return (display_dict, brief_str)


# ── extract_major_events（晨报重大事件提取，含容错恢复） ──

_MAJOR_EVENTS_BLOCK_RE = re.compile(
    r"<!--MAJOR_EVENTS_START-->\s*\n?(.*?)\n?\s*<!--MAJOR_EVENTS_END-->",
    re.DOTALL,
)
_MAJOR_EVENTS_ARRAY_RE = re.compile(r"\[\s*\{.*?\}\s*\]", re.DOTALL)
# 扁平 JSON 对象（major_event 不含嵌套花括号），用于整块解析失败时的逐对象恢复
_JSON_FLAT_OBJECT_RE = re.compile(r"\{[^{}]*\}")

_VALID_MAJOR_EVENT_DIRECTIONS = frozenset({"positive", "negative"})


def _sanitize_major_event(raw: dict[str, object]) -> dict[str, object] | None:
    """校验并规整单个 major_event；不合规返回 None（由调用方跳过）。

    业务规则（与 Morning Prompt 保持一致）：
    - title / summary 必须为非空字符串，缺失即视为坏事件
    - direction 仅允许 positive / negative（大小写归一；neutral 等视为坏事件）
    - impact_score 转换为 float（缺失/非法默认 0.0，不据此丢弃——价值判断归 LLM）
    - url 允许为空字符串（保持兼容，不参与筛选）
    - involved_keywords 缺失/非列表时默认空数组
    """
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return None
    summary = raw.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None

    score_raw = raw.get("impact_score", 0.0)
    if isinstance(score_raw, bool):
        score_raw = 0.0
    try:
        impact_score = float(cast(int | float | str, score_raw))
    except (TypeError, ValueError):
        impact_score = 0.0

    direction = str(raw.get("direction", "")).strip().lower()
    if direction not in _VALID_MAJOR_EVENT_DIRECTIONS:
        return None

    url = raw.get("url")
    if not isinstance(url, str):
        url = ""
    keywords = raw.get("involved_keywords")
    if not isinstance(keywords, list):
        keywords = []

    return {
        "title": title.strip(),
        "summary": summary.strip(),
        "url": url,
        "impact_score": impact_score,
        "direction": direction,
        "involved_keywords": keywords,
    }


def _sanitize_major_events(events: list[object]) -> list[dict[str, object]]:
    """批量规整 major_event 列表，丢弃不合规事件。"""
    result: list[dict[str, object]] = []
    for raw in events:
        if not isinstance(raw, dict):
            continue
        sane = _sanitize_major_event(raw)
        if sane is not None:
            result.append(sane)
    return result


def _recover_major_events_from_block(
    block: str, *, source: str
) -> list[dict[str, object]]:
    """从 JSON 块中提取合法 major_event；整体解析失败时逐对象容错恢复。

    核心目标：单个事件的格式问题（如 summary 含未转义中文引号）不得导致全量事件
    丢失——能解析出的合法事件继续返回。恢复日志记录原始块摘要/失败原因/位置/
    恢复数量，便于生产追踪。
    """
    recovery_info: dict[str, object] | None = None
    try:
        events = json.loads(block)
        if isinstance(events, list):
            sanitized = _sanitize_major_events(events)
            if len(sanitized) < len(events):
                logger.warning(
                    "major_events_dropped_invalid",
                    source=source,
                    total=len(events),
                    valid=len(sanitized),
                )
            return sanitized
        return []
    except (json.JSONDecodeError, TypeError) as exc:
        # 记录失败详情，供逐对象恢复后的追踪日志使用
        recovery_info = {
            "error_type": type(exc).__name__,
            "error_reason": str(exc)[:200],
            "error_position": getattr(exc, "pos", None),
            "error_line": getattr(exc, "lineno", None),
        }

    # 整体解析失败 → 按扁平对象逐个解析，仅保留可解析且字段合法的事件
    recovered: list[dict[str, object]] = []
    total = 0
    broken = 0
    for obj_text in _JSON_FLAT_OBJECT_RE.findall(block):
        total += 1
        try:
            obj = json.loads(obj_text)
        except (json.JSONDecodeError, TypeError):
            broken += 1
            continue
        sane = _sanitize_major_event(obj) if isinstance(obj, dict) else None
        if sane is None:
            broken += 1
            continue
        recovered.append(sane)

    logger.warning(
        "major_events_partial_recovery",
        source=source,
        error_type=str(recovery_info.get("error_type", "")),
        error_reason=str(recovery_info.get("error_reason", "")),
        error_position=recovery_info.get("error_position"),
        error_line=recovery_info.get("error_line"),
        total_objects=total,
        recovered=len(recovered),
        broken=broken,
        block_preview=block[:200],
    )
    return recovered


def extract_major_events(text: str) -> list[dict[str, object]]:
    """从晨报文本中提取重大事件列表。

    解析策略（逐级回退）：
    1. 查找 ``<!--MAJOR_EVENTS_START-->...<!--MAJOR_EVENTS_END-->`` 标记块
       - 整体 JSON 解析成功 → 规整后返回
       - 整体解析失败 → 逐对象容错恢复，丢弃格式损坏的事件，保留其余合法事件
    2. 兼容：正则匹配 JSON 数组 ``[{...}]``（同样容错恢复）
    3. 都失败返回空列表

    从 ``agents/workers/morning.py`` 迁出，供 morning run() 和 snapshot_builder 复用。
    """
    # 策略 1: 标记块
    match = _MAJOR_EVENTS_BLOCK_RE.search(text)
    if match:
        return _recover_major_events_from_block(match.group(1), source="marker_block")

    # 策略 2: 兼容 JSON 数组
    json_match = _MAJOR_EVENTS_ARRAY_RE.search(text)
    if json_match:
        return _recover_major_events_from_block(json_match.group(0), source="json_array")

    return []


# ── 通用 JSON 解析（供 event.py 各 helper 复用） ──


def _parse_json(text: str) -> dict[str, object] | list[object] | None:
    """从 LLM 输出文本中提取 JSON 对象或数组。

    解析策略（与 parse_event_output 一致）：
    1. 去掉 markdown 代码块（```json ... ``` 或 ``` ... ```）
    2. 整段 JSON 解析
    3. 正则匹配 JSON 块（花括号/方括号平衡）
    4. 都失败返回 None
    """
    if not text:
        return None

    # 去掉 markdown 代码块
    cleaned = re.sub(r'```(?:json)?\s*\n?', '', text)
    cleaned = re.sub(r'\n?\s*```', '', cleaned)
    cleaned = cleaned.strip()

    # 策略 1: 整段解析
    try:
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict | list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    # 策略 2: 正则匹配 JSON 块
    for pattern in [r'\{.*\}', r'\[.*\]']:
        match = re.search(pattern, cleaned, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
                if isinstance(parsed, dict | list):
                    return parsed
            except (json.JSONDecodeError, TypeError):
                pass

    logger.warning("json_parse_failed", text_preview=text[:200])
    return None


# ── 方向映射 ──

_DIRECTION_MAP: dict[str, str] = {
    "bullish": "bullish",
    "bearish": "bearish",
    "neutral": "neutral",
    "利好": "bullish",
    "利空": "bearish",
    "中性": "neutral",
    "positive": "positive",
    "negative": "negative",
}


def _normalize_direction(value: str, field: str) -> str:
    """方向值标准化，未知值打 log 后降级为 neutral"""
    normalized = _DIRECTION_MAP.get(value)
    if normalized is None:
        logger.warning("direction_normalize_fallback", field=field, raw=value)
        return "neutral"
    return normalized


def _as_list(value: object) -> list[object]:
    """从 dict.get() 结果中安全提取 list，非 list 值返回空列表。

    供 transform_to_frontend 内部列表推导使用，避免 mypy strict 下
    dict[str, object].get() 返回 object 不可迭代的问题。
    """
    return value if isinstance(value, list) else []


_INDUSTRY_GRAPH_MISSING_BOUNDARY = "本次未取得 IndustryKG 图谱事实，上下游关系未展开，不能补造。"
_INDUSTRY_GRAPH_DIRECT_RELATION_BOUNDARY = (
    "仅一跳直接关系，方向和强度是分析推断，不构成确定因果。"
)
_INDUSTRY_GRAPH_DEGRADED_STATUSES = {
    "not_queried",
    "invalid_input",
    "not_found",
    "authentication_failed",
    "upstream_failed",
    "timeout",
    "request_failed",
    "invalid_response",
}


def _degraded_industry_graph_evidence(
    status: str,
    missing_boundary: object = None,
) -> dict[str, object]:
    """构造未取得 IndustryKG 事实时的统一证据边界。"""
    if isinstance(missing_boundary, str) and missing_boundary.strip():
        boundary = missing_boundary
    else:
        boundary = _INDUSTRY_GRAPH_MISSING_BOUNDARY
    return {
        "status": status,
        "degraded": True,
        "scope": "one_hop",
        "source": None,
        "industry": None,
        "upstream": None,
        "downstream": None,
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": boundary,
    }


def _normalize_industry_graph_evidence(value: object) -> list[dict[str, object]]:
    """规范化仅由 Transmission 工具消息注入的 IndustryKG 证据。"""
    raw_evidence = value if isinstance(value, list) else []
    evidence: list[dict[str, object]] = []

    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status == "found":
            source = item.get("source")
            industry = item.get("industry")
            upstream = item.get("upstream")
            downstream = item.get("downstream")
            if _is_valid_found_industry_graph_evidence(
                item, industry, upstream, downstream
            ):
                evidence.append(
                    {
                        "status": "found",
                        "degraded": False,
                        "scope": "one_hop",
                        "source": source,
                        "industry": industry,
                        "upstream": upstream,
                        "downstream": downstream,
                        "graphVersion": item.get("graphVersion"),
                        "updatedAt": item.get("updatedAt"),
                        "missingBoundary": None,
                    }
                )
                continue
            evidence.append(_degraded_industry_graph_evidence("invalid_response"))
            continue

        normalized_status = (
            status if isinstance(status, str) and status in _INDUSTRY_GRAPH_DEGRADED_STATUSES
            else "invalid_response"
        )
        evidence.append(
            _degraded_industry_graph_evidence(
                normalized_status,
                item.get("missingBoundary"),
            )
        )

    return evidence or [_degraded_industry_graph_evidence("not_queried")]


def _is_valid_found_industry_graph_evidence(
    evidence: dict[object, object],
    industry: object,
    upstream: object,
    downstream: object,
) -> bool:
    """校验可用于约束链路的 IndustryKG 一跳 found 证据。"""
    if (
        evidence.get("scope") != "one_hop"
        or evidence.get("degraded") is not False
        or evidence.get("source") != "IndustryKGService"
        or not isinstance(upstream, list)
        or not isinstance(downstream, list)
    ):
        return False
    return (
        _is_valid_industry_node(industry)
        and all(_is_valid_industry_node(node, requires_leading_stocks=True) for node in upstream)
        and all(
            _is_valid_industry_node(node, requires_leading_stocks=True)
            for node in downstream
        )
    )


def _is_valid_industry_node(value: object, *, requires_leading_stocks: bool = False) -> bool:
    """校验 IndustryKG 行业节点的最小身份字段。"""
    if not isinstance(value, dict):
        return False
    industry_id = value.get("id")
    name = value.get("name")
    if not (
        isinstance(industry_id, str)
        and industry_id.strip()
        and isinstance(name, str)
        and name.strip()
    ):
        return False
    return not requires_leading_stocks or isinstance(value.get("leadingStocks"), list)


def _industry_names(nodes: object) -> set[str]:
    """从一侧 IndustryKG 节点提取可验证的行业名称。"""
    if not isinstance(nodes, list):
        return set()
    return {
        name
        for node in nodes
        if isinstance(node, dict)
        for name in [node.get("name")]
        if isinstance(name, str) and name
    }


def _as_string_keyed_dict(value: object) -> dict[str, object] | None:
    """将 JSON 对象收窄为字符串键的字典。"""
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in value):
        return None
    return value


def _constrain_chain_by_industry_graph(
    chain: list[object],
    evidence: list[dict[str, object]],
) -> list[dict[str, object]]:
    """用一跳图谱事实约束模型输出的行业链节。

    Phase 1 fail-safe 规则：
    - 核心行业在任何异常路径下必须保留（标注 kg_unverified=true）。
    - 图谱成功时追加邻接行业（与当前逻辑一致）。
    - relation 校验放宽：含"核心"或 level==1 均视为核心行业。
    """
    found_evidence = [item for item in evidence if item["status"] == "found"]

    # ── 分支 1：无有效图谱证据 → fail-safe 保留全部核心行业 ──
    if not found_evidence:
        core_chain: list[dict[str, object]] = []
        for raw_item in chain:
            item = _as_string_keyed_dict(raw_item)
            if item is None:
                continue
            # 放宽 relation 匹配：核心行业 / 中心行业 / level==1 均视为核心
            if _is_core_industry(item):
                core_chain.append({
                    **item,
                    "relation": "核心行业",
                    "level": 1,
                    "kg_unverified": True,
                })
        return core_chain

    # ── 分支 2：有图谱证据 → 建立邻接表 + 约束邻接行业 ──
    center_adjacency: dict[str, dict[str, set[str]]] = {}
    center_ids_by_name: dict[str, set[str]] = {}
    for item in found_evidence:
        industry = item["industry"]
        if not isinstance(industry, dict):
            continue
        center_id = industry.get("id")
        center_name = industry.get("name")
        if not (
            isinstance(center_id, str)
            and center_id
            and isinstance(center_name, str)
            and center_name
        ):
            continue
        center_adjacency[center_id] = {
            "upstream": _industry_names(item["upstream"]),
            "downstream": _industry_names(item["downstream"]),
        }
        center_ids_by_name.setdefault(center_name, set()).add(center_id)

    constrained: list[dict[str, object]] = []
    current_center_id: str | None = None
    for raw_item in chain:
        item = _as_string_keyed_dict(raw_item)
        if item is None:
            continue
        if _is_core_industry(item):
            current_center_id = None
            industry_name = item.get("industry")
            center_ids = (
                center_ids_by_name.get(industry_name)
                if isinstance(industry_name, str)
                else None
            )
            if center_ids is not None and len(center_ids) == 1:
                # 名称唯一匹配 → 正常追加
                current_center_id = next(iter(center_ids))
                constrained.append({**item, "relation": "核心行业", "level": 1})
            else:
                # 名称不匹配或同名多 ID → fail-safe 保留核心行业并标记
                constrained.append({
                    **item,
                    "relation": "核心行业",
                    "level": 1,
                    "kg_unverified": True,
                })
            continue

        industry = item.get("industry")
        if not isinstance(industry, str) or current_center_id is None:
            continue
        adjacency = center_adjacency[current_center_id]
        if industry in adjacency["upstream"]:
            constrained.append(
                {
                    **item,
                    "relation": "图谱上游（直接关系）",
                    "level": 2,
                    "reason": _INDUSTRY_GRAPH_DIRECT_RELATION_BOUNDARY,
                }
            )
        elif industry in adjacency["downstream"]:
            constrained.append(
                {
                    **item,
                    "relation": "图谱下游（直接关系）",
                    "level": 2,
                    "reason": _INDUSTRY_GRAPH_DIRECT_RELATION_BOUNDARY,
                }
            )
    return constrained


def _is_core_industry(item: dict[str, object]) -> bool:
    """判断 chain 条目是否为核心行业（兼容多种 relation 格式）。"""
    relation = item.get("relation")
    if isinstance(relation, str) and "核心" in relation:
        return True
    level = item.get("level")
    if isinstance(level, int | float) and level == 1:
        return True
    return False


def _build_evidence_candidate_set(
    evidence: list[dict[str, object]],
) -> set[str]:
    """从 industryGraphEvidence 中提取合法的候选行业名集合。

    用于事后校验 chain 中的行业是否全部来自图谱候选。
    核心行业名也被纳入候选集（KG 查询时就是按它匹配的）。
    """
    candidates: set[str] = set()
    for item in evidence:
        if item.get("status") != "found":
            continue
        industry = item.get("industry")
        if isinstance(industry, dict):
            name = industry.get("name")
            if isinstance(name, str) and name.strip():
                candidates.add(name.strip())
        for side in ("upstream", "downstream"):
            side_list = item.get(side)
            if not isinstance(side_list, list):
                continue
            for node in side_list:
                if isinstance(node, dict):
                    name = node.get("name")
                    if isinstance(name, str) and name.strip():
                        candidates.add(name.strip())
    return candidates


def _validate_chain_against_evidence(
    chain: list[dict[str, object]],
    evidence: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Phase 2 代码校验：标注 chain 中不在图谱候选集内的行业。

    校验规则（非破坏性——核心行业永远保留）：
    - 无 found 证据时跳过校验（Phase 1 fail-safe 已处理）。
    - 有 found 证据时：核心行业不在候选集 → 标记 kg_unverified。
    - 邻接行业不在候选集 → 标记 kg_unverified（不删除，保留 LLM 判断但标低置信度）。

    返回：
        可能附带了 kg_unverified 标记的 chain 副本。
    """
    candidates = _build_evidence_candidate_set(evidence)
    if not candidates:
        # 无有效证据候选集 → 不校验
        return chain

    validated: list[dict[str, object]] = []
    for item in chain:
        industry = item.get("industry")
        if not isinstance(industry, str) or not industry.strip():
            validated.append(item)
            continue

        in_candidates = industry.strip() in candidates
        is_core = _is_core_industry(item)

        if not in_candidates:
            # 不在候选集中：标记 kg_unverified
            validated.append({**item, "kg_unverified": True})
        elif is_core and not item.get("kg_unverified", False):
            # 核心行业在候选集中且未标记 → 确认无 kg_unverified
            validated.append(item)
        else:
            validated.append(item)

    return validated


def _build_chain_industry_set(chain: list[dict[str, object]]) -> set[str]:
    """提取 transmission.chain 中所有非空行业名（strip 后）。

    用于投资机会一致性校验：investment.focusIndustries 必须能追溯到 chain。
    """
    names: set[str] = set()
    for item in chain:
        industry = item.get("industry")
        if isinstance(industry, str) and industry.strip():
            names.add(industry.strip())
    return names


def _industry_traceable(name: str, chain_names: set[str]) -> bool:
    """判断 investment 行业是否可追溯到 chain 行业。

    兼容行业粒度差异：chain=半导体 → focus=半导体制造/半导体设备 应视为可追溯。
    采用双向子串包含匹配（chain_name in name 或 name in chain_name），
    不做字符串完全相等——避免误杀同源细分行业。
    """
    name = name.strip()
    if not name:
        return False
    for chain_name in chain_names:
        if chain_name in name or name in chain_name:
            return True
    return False


def _filter_focus_industries_by_chain(
    focus_industries: list[dict[str, object]],
    chain_names: set[str],
) -> list[dict[str, object]]:
    """仅保留能追溯到 chain 行业的 focusIndustries 条目。

    chain_names 为空时返回空列表（投资机会必须可追溯到传导链）。
    """
    if not chain_names:
        return []
    result: list[dict[str, object]] = []
    for fi in focus_industries:
        name = fi.get("name")
        if isinstance(name, str) and _industry_traceable(name, chain_names):
            result.append(fi)
        else:
            logger.info(
                "investment_focus_industry_filtered",
                industry=name,
                chain=list(chain_names),
            )
    return result


# ── 字段映射 ──


def transform_to_frontend(
    understanding: dict[str, object] | None,
    transmission: dict[str, object] | None,
    history: list[object] | None,
    investment: dict[str, object] | None,
    event_meta: dict[str, object],
) -> dict[str, object]:
    """将 4 个 LLM 模块输出 + 事件元信息映射为 analysis_reports。

    Args:
        understanding: Call 1 输出（EventUnderstanding JSON dict）
        transmission: Call 2 输出（TransmissionAnalysis JSON dict）
        history: Call 3 输出（HistoryEvent[] list）
        investment: Call 4 输出（InvestmentSummary JSON dict）
        event_meta: {"eventId": str, "title": str, "source": str}

    Returns:
        analysis_reports dict，结构：
        {
            "event_understanding": {...},
            "event_transmission": {...},
            "event_history": [...],
            "event_investment": {...},
        }
    """
    reports: dict[str, object] = {}
    # 传导链行业集合：供 event_investment 后置校验（investment 行业必须可追溯到 chain）
    chain_names: set[str] = set()

    # ── event_understanding ──
    if understanding and isinstance(understanding, dict):
        reports["event_understanding"] = {
            "summary": str(understanding.get("summary", "")),
            # 短标题（2026-08-14）：透传 LLM 独立生成的 title，供缓存幂等补写
            # 复用同一标题；前端仍消费顶层 content.title，本字段仅内部一致性用途。
            "title": str(understanding.get("title", "")),
            "coreIndustry": str(understanding.get("coreIndustry", "")),
            "coreChanges": [
                {
                    "variable": str(c.get("variable", "")),
                    "before": str(c.get("before", "")),
                    "after": str(c.get("after", "")),
                }
                for c in _as_list(understanding.get("coreChanges", []))
                if isinstance(c, dict)
            ],
            # 第四阶段：事件传导价值判断（Call1 语义判断结果，随理解落库供后续读取）
            "is_stock_only": bool(understanding.get("is_stock_only", False)),
            "transmission_needed": bool(understanding.get("transmission_needed", True)),
            "transmission_reason": str(understanding.get("transmission_reason", "")),
        }
    else:
        reports["event_understanding"] = None

    # ── event_transmission ──
    if transmission and isinstance(transmission, dict):
        variables = _as_list(transmission.get("variables", []))
        industry_graph_evidence = _normalize_industry_graph_evidence(
            transmission.get("industryGraphEvidence")
        )
        chain = _constrain_chain_by_industry_graph(
            _as_list(transmission.get("chain", [])),
            industry_graph_evidence,
        )
        # Phase 2 代码校验：标注不在图谱候选集内的行业为 kg_unverified
        chain = _validate_chain_against_evidence(chain, industry_graph_evidence)
        core_industry = transmission.get("coreIndustry", {})

        # 代码确定性排序：按 impactStrength 降序（不改变 impactStrength/reason，仅调整顺序）。
        # 防止 LLM 输出顺序影响前端展示——前端第一行业一定是事件影响最大的行业。
        chain_items: list[dict[str, object]] = [
            {
                "industry": str(c.get("industry", "")),
                "relation": str(c.get("relation", "核心行业")),
                "level": int(cast(str | float | int, c.get("level", 1))),
                "direction": _normalize_direction(
                    str(c.get("direction", "")), "chain.direction"
                ),
                "impactStrength": float(
                    cast(str | float | int, c.get("impactStrength", 0))
                ),
                "reason": str(c.get("reason", "")),
                # Phase 1 fail-safe：图谱未验证标记（前端可选消费）
                "kg_unverified": bool(c.get("kg_unverified", False)),
            }
            for c in chain
            if isinstance(c, dict)
        ]
        chain_items.sort(key=lambda item: item["impactStrength"], reverse=True)
        chain_names = _build_chain_industry_set(chain_items)

        reports["event_transmission"] = {
            "eventId": event_meta.get("eventId", ""),
            "mechanism": str(transmission.get("mechanism", "")),
            "variables": [
                {
                    "name": str(v.get("name", "")),
                    "direction": _normalize_direction(
                        str(v.get("direction", "")), "variables.direction"
                    ),
                    "strength": float(v.get("strength", 0)),
                    "explanation": str(v.get("explanation", "")),
                }
                for v in variables
                if isinstance(v, dict)
            ],
            "coreIndustry": {
                "name": str(core_industry.get("name", "")),
                "impact": str(core_industry.get("impact", "")),
                "reason": str(core_industry.get("reason", "")),
            } if isinstance(core_industry, dict) else {"name": "", "impact": "", "reason": ""},
            "industryGraphEvidence": industry_graph_evidence,
            "chain": chain_items,
        }
    else:
        reports["event_transmission"] = None

    # ── event_history ──
    if history and isinstance(history, list):
        reports["event_history"] = [
            {
                "historyId": str(h.get("historyId", "")),
                "year": str(h.get("year", "")),
                "title": str(h.get("title", "")),
                "eventType": str(h.get("eventType", "")),
                "sentiment": _normalize_direction(str(h.get("sentiment", "")), "history.sentiment"),
                "industryChange": str(h.get("industryChange", "")),
                "changePercentage": float(h.get("changePercentage", 0)),
            }
            for h in history
            if isinstance(h, dict)
        ]
    else:
        reports["event_history"] = []

    # ── event_investment ──
    if investment and isinstance(investment, dict):
        focus_industries = _as_list(investment.get("focusIndustries", []))
        # 后置一致性校验：投资机会行业必须可追溯到 transmission.chain。
        # chain 为空时强制清空 focusIndustries/opportunities 并降级为 neutral，
        # 防止 investment 仅凭 industryGraphEvidence 或关键词独立生成行业（脱节）。
        filtered_focus = _filter_focus_industries_by_chain(
            [
                fi
                for fi in focus_industries
                if isinstance(fi, dict)
            ],
            chain_names,
        )
        if not chain_names:
            opportunities: list[str] = []
            rating = "neutral"
            conclusion = str(investment.get("conclusion", ""))
            # 仅当 LLM 给出了具体行业结论时才覆盖为"未形成明确传导"文案，
            # 避免覆盖本就为空或已正确的 neutral 结论。
            if "受益" in conclusion or "承压" in conclusion or "景气" in conclusion:
                conclusion = "事件未形成明确行业传导，暂不提供具体行业投资机会"
        else:
            opportunities = [
                str(o) for o in _as_list(investment.get("opportunities", []))
            ]
            rating = _normalize_direction(str(investment.get("rating", "neutral")), "rating")
            conclusion = str(investment.get("conclusion", ""))

        reports["event_investment"] = {
            "id": event_meta.get("eventId", ""),
            "conclusion": conclusion,
            "keyPoints": [
                str(kp) for kp in _as_list(investment.get("keyPoints", []))
            ],
            "focusIndustries": [
                {
                    "name": str(fi.get("name", "")),
                    "direction": _normalize_direction(
                        str(fi.get("direction", "")), "focusIndustries.direction"
                    ),
                    "reason": str(fi.get("reason", "")),
                }
                for fi in filtered_focus
            ],
            "opportunities": opportunities,
            "risks": [
                str(r) for r in _as_list(investment.get("risks", []))
            ],
            "rating": rating,
        }
    else:
        reports["event_investment"] = None

    return reports
