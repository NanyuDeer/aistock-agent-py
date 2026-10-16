"""event_scope 单一裁决（STOCK > MARKET > UNKNOWN）。"""

from __future__ import annotations

from typing import Any

from aistock_agent.services.event_origin import OVERSEAS_MACRO_KEYWORDS
from aistock_agent.services.stock_event_detector import detect_stock_event

_RANK: dict[str, int] = {"STOCK": 3, "MARKET": 2, "UNKNOWN": 1}

# 市场级 token（与 attribution_chain._MARKET_WIDE_TOKENS 同源口径）
_MARKET_TOKENS: tuple[str, ...] = (
    "沪指", "上证指数", "深证成指", "创业板指", "科创50", "沪深300",
    "大盘", "两市", "A股", "全市场",
)


def classify_event_scope(source: str, title: str, summary: str) -> str:
    """子判定：市场/大盘级 → MARKET，否则 UNKNOWN（不判 STOCK）。"""
    text = f"{title} {summary}"
    if str(source) in ("jin10", "global_markets"):
        return "MARKET"
    if any(k in text for k in OVERSEAS_MACRO_KEYWORDS) or any(k in text for k in _MARKET_TOKENS):
        return "MARKET"
    return "UNKNOWN"


def resolve_event_scope(
    source: str, title: str, summary: str, payload: Any
) -> tuple[str, str, float]:
    """唯一合成：STOCK > MARKET > UNKNOWN；胜者身兼 source/confidence。"""
    d = detect_stock_event(title, summary, payload, source)  # STOCK|UNKNOWN
    m_scope = classify_event_scope(source, title, summary)
    if _RANK[d["event_scope"]] >= _RANK[m_scope]:
        return d["event_scope"], d["event_scope_source"], d["event_scope_confidence"]
    return "MARKET", "market_rule", 0.7
