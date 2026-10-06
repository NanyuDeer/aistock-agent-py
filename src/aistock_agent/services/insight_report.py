"""完整洞察报告章节构建（2026-09-13；2026-09-25 由 PDF 渲染改为结构化输出；
2026-09-26 章节内容由纯文本行改为结构化 blocks，支持表格/时间轴等呈现）。

纯模板填充，不调用 LLM：app-api 组装报告数据 → 本服务输出章节结构（JSON）
→ app-api 分块推 SSE → 前端按 block.type 渲染（六阶段因果链为纵向时间轴）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

_MISSING = "暂缺"


def _text(value: Any) -> str:
    """任意值 → 展示文本；空值统一"暂缺"。"""
    if value is None or value == "":
        return _MISSING
    return str(value)


_LAYER_LABELS = {
    "company": "公司层面",
    "sector": "板块层面",
    "market": "市场层面",
    "capital": "资金层面",
    "technical": "技术层面",
    "article": "资讯层面",
}

_CANDIDATE_STATUS_LABELS = {
    "supported": "已佐证",
    "weak": "佐证偏弱",
    "rejected": "已排除",
    "insufficient": "证据不足",
}

_CHAIN_STAGE_LABELS = {
    "structural_root": "结构根因",
    "trigger": "触发",
    "transmission": "传导",
    "exposure": "暴露",
    "repricing": "重定价",
    "observable_result": "可观察结果",
}

_EPISTEMIC_LABELS = {
    "fact": "事实",
    "inference": "推断",
    "hypothesis": "假设",
}

_CHAIN_STATUS_LABELS = {
    "established": "已确立",
    "partial": "部分确立",
    "not_established": "未确立",
}

_DIRECTION_LABELS = {"up": "上涨", "down": "下跌"}

# 严重度与置信度共用同一档位词表（critical 仅严重度会出现）
_LEVEL_LABELS = {
    "critical": "极高",
    "high": "高",
    "medium": "中",
    "low": "低",
}

# 证据行的来源标识中文化：kind 有权威枚举（types.ts::SourceKind，9 个）；
# provider 是自由字符串，未登记取值由 `_label` 原样输出（不丢信息）。
# source_id 与 source_level 保留原始值——可溯源、与库中记录一一对应。
_PROVIDER_LABELS = {
    "ths_limit_up_radar": "同花顺涨停雷达",
    "ths": "同花顺",
    "tushare_moneyflow": "Tushare 资金流",
    "tushare": "Tushare",
    "eastmoney_kline": "东方财富 K 线",
    "eastmoney": "东方财富",
    "tencent_quote": "腾讯行情",
    "tencent_index": "腾讯指数",
    "cls": "财联社",
    "stock_trace_detector": "异动检测器",
    "stock_info": "资讯中台",
}

_KIND_LABELS = {
    "trigger_fact": "触发事实",
    "quote_fact": "行情事实",
    "sector_fact": "板块事实",
    "market_fact": "大盘事实",
    "capital_fact": "资金事实",
    "technical_fact": "技术事实",
    "announcement": "公告",
    "news": "新闻",
    "insight_article": "情报文章",
}

# LLM 正文里会原样引用数据域/字段名（如"快照缺失 technical_context"、"trade_date为…"），展示层翻译
_TERM_LABELS = {
    "company_context": "公司面数据",
    "sector_context": "板块面数据",
    "market_context": "市场面数据",
    "capital_context": "资金面数据",
    "technical_context": "技术面数据",
    "article_context": "资讯面数据",
    "trade_date": "交易日",
}
_TERM_PATTERN = re.compile("|".join(re.escape(key) for key in _TERM_LABELS))

# compact 日期（20260923）→ 2026-09-23。用 lookaround 而非 \b：中文属 \w，"为20260923" 处没有词边界
_COMPACT_DATE_PATTERN = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")

# 存量证据的英文题名（app-api StockTraceSnapshotService 生成时写库，历史数据仍是英文）
_EXCERPT_TITLES = {
    "Price trigger event": "价格触发事件",
    "Trigger-time quote fact": "触发时点行情",
    "Corrected price trigger event": "修正后价格触发事件",
    "Corrected trigger-time quote fact": "修正后触发时点行情",
}

_ExcerptRepl = str | Callable[[re.Match[str]], str]


def _board_excerpt(match: re.Match[str]) -> str:
    """板块证据摘要：compact 日期 20260923 → 2026-09-23。"""
    raw = match.group(2)
    day = f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}" if len(raw) == 8 else raw
    return f"板块最新日涨跌幅 {match.group(1)}%（{day}）。"


# 存量证据的英文摘要模板。**不锚定行首、锚定行尾**（证据行里摘要恒为末段），
# 未命中的文本原样保留，避免误伤正常内容。
_EXCERPT_PATTERNS: list[tuple[re.Pattern[str], _ExcerptRepl]] = [
    (
        re.compile(r"Price change ([\d.]+)% crossed ([\d.]+)% threshold\.$"),
        r"涨跌幅 \1%，突破 \2% 阈值。",
    ),
    (
        re.compile(r"Trigger revision (\d+); price change (-?[\d.]+)%\.$"),
        r"触发修订 \1；涨跌幅 \2%。",
    ),
    (
        re.compile(r"Latest (.+?); previous close (.+?); change (-?[\d.]+)%\.$"),
        r"最新价 \1；昨收 \2；涨跌幅 \3%。",
    ),
    (re.compile(r"Board latest daily change (-?[\d.]+)% on (\d{8})\.$"), _board_excerpt),
    (re.compile(r"(.+?) change (-?[\d.]+)%\.$"), r"\1 涨跌幅 \2%。"),
]

_CN_TZ = timezone(timedelta(hours=8))


def _label(value: Any, mapping: dict[str, str]) -> str:  # noqa: ANN401 - 入参为原始 JSON 值
    """枚举值 → 中文标签；缺失"暂缺"，未登记取值原样输出（不丢信息）。"""
    if value is None or value == "":
        return _MISSING
    return mapping.get(str(value), str(value))


def _time_text(value: Any) -> str:  # noqa: ANN401 - 入参为原始 JSON 值
    """ISO 时间 → 上海时区「YYYY-MM-DD HH:mm」；无法解析时原样输出。"""
    if value is None or value == "":
        return _MISSING
    text = str(value)
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(_CN_TZ).strftime("%Y-%m-%d %H:%M")


def _localize(text: str) -> str:
    """展示层中文化（**只影响 PDF 显示，不改库**）：

    1. 证据题名/摘要的英文模板 —— 生成时写库，历史数据仍是英文；
    2. LLM 正文里残留的数据域/字段名（company_context、trade_date …）；
    3. compact 日期（20260923 → 2026-09-23）。

    未命中任何规则的文本原样返回。
    """
    out = text
    # 长键优先，避免 "Corrected price trigger event" 之类被短键抢先命中
    titles_by_length = sorted(_EXCERPT_TITLES.items(), key=lambda kv: len(kv[0]), reverse=True)
    for english, chinese in titles_by_length:
        out = out.replace(english, chinese)
    for pattern, repl in _EXCERPT_PATTERNS:
        if pattern.search(out):
            out = pattern.sub(repl, out)
            break
    out = _TERM_PATTERN.sub(lambda m: _TERM_LABELS[m.group(0)], out)
    return _COMPACT_DATE_PATTERN.sub(r"\1-\2-\3", out)


def build_report_header(data: dict[str, Any]) -> str:
    """页眉一行：股票名（代码） · 交易日；字段缺失统一"暂缺"。"""
    event = data.get("event") or {}
    return (
        f"{_text(event.get('stockName'))}（{_text(event.get('symbol'))}）"
        f" · {_text(event.get('tradingDate'))}"
    )


def _evidence_id_list(ids: Any) -> list[str]:  # noqa: ANN401 - 入参为原始 JSON 值
    """证据 ID 列表 → 清洗后的字符串列表（去空值）。"""
    if not isinstance(ids, list):
        return []
    return [str(i) for i in ids if i]


def _tone(value: Any) -> str | None:  # noqa: ANN401 - 入参为原始 JSON 值
    """方向 → 涨跌着色标记（up/down）；其它取值不着色。"""
    return value if value in ("up", "down") else None


def _change_text(event: dict[str, Any]) -> str:
    """涨跌幅展示文本：`7.69%（阈值 7%）`。"""
    pct = _text(event.get("changePct"))
    threshold = _text(event.get("thresholdPct"))
    return f"{pct}%（阈值 {threshold}%）"


def _primary_chain(chains: Any) -> dict[str, Any] | None:  # noqa: ANN401 - 入参为原始 JSON 值
    """取主链（role=primary）。

    真实数据均有 primary；缺失时降级取第一条链，避免整节消失。
    非 dict 的链（脏数据）直接跳过。
    """
    if not isinstance(chains, list):
        return None
    valid = [c for c in chains if isinstance(c, dict)]
    for chain in valid:
        if chain.get("role") == "primary":
            return chain
    return valid[0] if valid else None


def _chain_stages(attr: dict[str, Any]) -> list[dict[str, Any]]:
    """主链 → 六阶段时间轴节点列表（顺序即因果顺序）。

    每个节点同时给出中文标签与机器 key：标签用于展示，
    key 供前端做"中性弱化"判定（不对中文标签做字符串匹配）。
    """
    chain = _primary_chain(attr.get("chains"))
    if chain is None:
        return []
    stages: list[dict[str, Any]] = []
    for node in chain.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        evidence_ids = _evidence_id_list(node.get("evidenceIds"))
        stages.append({
            "stage": _label(node.get("stage"), _CHAIN_STAGE_LABELS),
            "stageKey": _text(node.get("stage")),
            "claim": _localize(_text(node.get("claim"))),
            "epistemic": _label(node.get("epistemicType"), _EPISTEMIC_LABELS),
            "epistemicKey": _text(node.get("epistemicType")),
            "status": _label(node.get("status"), _CHAIN_STATUS_LABELS),
            "statusKey": _text(node.get("status")),
            "evidenceIds": evidence_ids,
            "evidenceCount": len(evidence_ids),
        })
    return stages


def _with_items(block_type: str, items: list[Any]) -> list[dict[str, Any]]:
    """空列表 → 空 blocks（前端渲染"暂缺"），避免出现 `items: []` 的空壳 block。"""
    return [{"type": block_type, "items": items}] if items else []


def _with_stages(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """六阶段因果链 block：只含主链，空则空 blocks。"""
    return [{"type": "chain", "stages": stages}] if stages else []


def build_report_blocks(data: dict[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
    """报告数据 → 章节列表 (标题, blocks)。缺数据的章节输出空 blocks（供前端渲染"暂缺"）。

    结构化 blocks（判别联合，`type` 区分）供前端按类型渲染（表格/时间轴/卡片）：
    - `kv`         : 事件事实（键值两列）
    - `verdict`    : 主因结论（正文 + 徽标）
    - `candidates` : 分层候选归因
    - `chain`      : 六阶段因果链（**只含主链**）
    - `evidence`   : 证据清单
    - `list`       : 未解问题

    枚举值（层/状态/方向/严重度/置信度）与时间统一转为中文可读形式；
    自由文本再过一遍 `_localize`（覆盖存量数据里的英文模板与字段名）。
    """
    event = data.get("event") or {}
    attr = data.get("attribution") or {}

    # ── 事件事实（kv）：页眉已有「股票名（代码）」，此处不重复 ──
    event_items: list[dict[str, Any]] = []
    if event:
        direction = event.get("direction")
        event_items = [
            {"label": "触发时间", "value": _time_text(event.get("triggeredAt"))},
            {"label": "方向", "value": _label(direction, _DIRECTION_LABELS),
             "tone": _tone(direction)},
            {"label": "涨跌幅", "value": _change_text(event), "tone": _tone(direction)},
            {"label": "严重度", "value": _label(event.get("severity"), _LEVEL_LABELS)},
            {"label": "最新价", "value": _text(event.get("latestPrice"))},
            {"label": "昨收", "value": _text(event.get("previousClose"))},
        ]

    # ── 主因结论（verdict）──
    verdict_blocks: list[dict[str, Any]] = []
    if attr:
        verdict_blocks = [{
            "type": "verdict",
            "text": _localize(_text(attr.get("primaryPhrase"))),
            "badges": [
                {"label": "置信度", "value": _label(attr.get("confidenceLevel"), _LEVEL_LABELS)},
                {"label": "归类标签", "value": _label(attr.get("primaryLayer"), _LAYER_LABELS)},
                {"label": "归因生成时间", "value": _time_text(attr.get("generatedAt"))},
            ],
        }]

    candidates = [c for c in (attr.get("candidates") or []) if isinstance(c, dict)]
    candidate_items = [{
        "layer": _label(c.get("layer"), _LAYER_LABELS),
        "status": _label(c.get("status"), _CANDIDATE_STATUS_LABELS),
        # 机器 key：供前端做中性弱化判定（statusKey !== 'supported' 即弱化），不参与展示
        "statusKey": _text(c.get("status")),
        "verdict": _localize(_text(c.get("verdict"))),
        "evidenceIds": _evidence_id_list(c.get("supportingEvidenceIds")),
    } for c in candidates]

    stages = _chain_stages(attr)

    evidence = [e for e in (attr.get("evidenceIndex") or []) if isinstance(e, dict)]
    evidence_items = [{
        "sourceId": _text(e.get("source_id")),
        "provider": _label(e.get("provider"), _PROVIDER_LABELS),
        "kind": _label(e.get("kind"), _KIND_LABELS),
        "occurredAt": _time_text(e.get("occurred_at")),
        "level": _text(e.get("source_level")),
        "title": _localize(_text(e.get("title"))),
        "excerpt": _localize(_text(e.get("content_excerpt"))),
    } for e in evidence]

    questions = [str(q) for q in (attr.get("unresolvedQuestions") or []) if q]

    return [
        ("事件事实", [{"type": "kv", "items": event_items}] if event_items else []),
        ("主因结论", verdict_blocks),
        ("分层候选归因", _with_items("candidates", candidate_items)),
        ("六阶段因果链", _with_stages(stages)),
        ("证据清单", _with_items("evidence", evidence_items)),
        ("未解问题", _with_items("list", questions)),
    ]


def build_report_response(data: dict[str, Any]) -> dict[str, Any]:
    """报告数据 → `{header, sections}`（供 app-api 分块推 SSE、前端逐章节渲染）。

    sections 元素为 `{heading, blocks}`；blocks 内文本已是中文化 + 时间格式化 +
    枚举翻译后的最终展示文本，前端只负责按 `block.type` 选择呈现形式。
    """
    return {
        "header": build_report_header(data),
        "sections": [
            {"heading": heading, "blocks": blocks}
            for heading, blocks in build_report_blocks(data)
        ],
    }
