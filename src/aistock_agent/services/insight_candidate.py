"""自选股洞察候选抽取（确定性规则）。

从单条异动新闻的 title（标题）/ keywords（标题解析关键词数组）/ content（正文）
中抽取 ``CandidateFactor`` 候选因子，供下游洞察组装使用。纯函数 + 静态字典，
无 IO 依赖、无 LLM 调用，结果确定可复现。

三路候选：
1. L1 正文结构信号（BODY_SIGNALS，如"行业原因："）→ source="body"，强度高
2. L1 正文"据XXX"事实引用 → source="body"，强度中
3. L3 标题关键词（insight_keywords.json 词典分类）→ source="title"，强度低

负向信号（NEGATIVE_SIGNALS，如"澄清/尚未/不存在"）命中正文时，正文派生的候选
被标记 suppressed（原因未确认或被否认，不得作为事实呈现给下游）。类别枚举严格
五类：industry_theme / company_event / earnings / market / trading_sentiment。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import structlog
from pydantic import BaseModel

logger = structlog.get_logger()

# 类别枚举：与自选股洞察 schema 严格一致
# （industry_theme / company_event / earnings / market / trading_sentiment）
CATEGORIES: tuple[str, ...] = (
    "industry_theme",
    "company_event",
    "earnings",
    "market",
    "trading_sentiment",
)

# 负向信号：命中正文即把相关候选标记 suppressed（异动原因被否认/尚未确认）
NEGATIVE_SIGNALS: tuple[str, ...] = (
    "澄清",
    "不存在",
    "尚未",
    "不涉及",
    "风险提示",
    "未取得",
    "预计",
    "不会",
    "无法确认",
    "否认",
)

# 正文结构信号：信号 → 主因分类（"行业原因：..." 等直接引用式正文）
BODY_SIGNALS: dict[str, str] = {
    "行业原因": "industry_theme",
    "公司原因": "company_event",
    "据公告": "company_event",
    "据业绩预告": "earnings",
    "业绩预告": "earnings",
    "据研报": "industry_theme",
    "隔夜美股": "market",
    "板块": "industry_theme",
    "订单": "company_event",
    "重组": "company_event",
    "定增": "company_event",
}

_KEYWORD_MAP: dict[str, str] | None = None


def _load_keyword_map() -> dict[str, str]:
    """惰性加载 标题关键词 → 分类 词典（insight_keywords.json，只读）。"""
    global _KEYWORD_MAP
    if _KEYWORD_MAP is None:
        keywords_file = Path(__file__).parent.parent / "data" / "insight_keywords.json"
        raw = json.loads(keywords_file.read_text(encoding="utf-8"))
        _KEYWORD_MAP = {kw: cat for cat, kws in raw.items() for kw in kws}
    return _KEYWORD_MAP


def classify_title_keyword(kw: str) -> str | None:
    """标题关键词 → 主因分类；词典未收录返回 None。"""
    return _load_keyword_map().get(kw)


class CandidateFactor(BaseModel):
    id: str
    label: str
    category: str
    source: str  # "body" | "title" | "quant"
    evidence_quote: str
    strength: float  # 0-1
    suppressed: bool = False
    suppress_reason: str | None = None


def _is_suppressed(text: str) -> tuple[bool, str | None]:
    """命中任一负向信号返回 (True, "negative_signal:<信号>")，否则 (False, None)。"""
    for sig in NEGATIVE_SIGNALS:
        if sig in text:
            return True, f"negative_signal:{sig}"
    return False, None


def extract_candidates(title: str, keywords: list[str], content: str) -> list[CandidateFactor]:
    """三路候选：正文结构信号（L1）+ 正文事实引用（L1 弱）+ 标题关键词（L3）。

    确定性规则，无 LLM 调用。负向信号命中正文时，正文派生的候选被 suppressed。
    """
    cands: list[CandidateFactor] = []
    seen: set[str] = set()

    def add(label: str, category: str, source: str, quote: str, strength: float) -> None:
        if label in seen:
            return
        seen.add(label)
        # 负向信号出现在正文 ⇒ 该正文派生的因果候选未确认（单条异动原因文本粒度，
        # 整段检查）；标题候选只查标题本身。
        if source == "body":
            suppressed, reason = _is_suppressed(content)
        else:
            suppressed, reason = _is_suppressed(quote)
        cands.append(
            CandidateFactor(
                id=f"c{len(cands) + 1}",
                label=label,
                category=category,
                source=source,
                evidence_quote=quote,
                strength=strength,
                suppressed=suppressed,
                suppress_reason=reason,
            )
        )

    # L1：正文结构信号（"行业原因：..." / "公司原因：..." 直接引用）
    for signal, category in BODY_SIGNALS.items():
        idx = content.find(signal)
        if idx >= 0:
            snippet = content[idx : idx + 120]
            add(
                f"{signal}:{snippet[:20]}",
                category,
                "body",
                snippet[:120],
                0.9 if signal in ("行业原因", "公司原因") else 0.7,
            )

    # L1：正文"据XXX"事实引用（提取事实来源短语）
    for m in re.finditer(r"(据[^，。；;]{2,40})", content):
        add(m.group(1)[:24], "company_event", "body", m.group(1)[:120], 0.6)

    # L3：标题关键词（词典分类）
    for kw in keywords:
        cat = classify_title_keyword(kw)
        if cat:
            add(kw, cat, "title", title, 0.3)

    return cands
