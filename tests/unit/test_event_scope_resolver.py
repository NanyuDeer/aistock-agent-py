"""event_scope 单一裁决单元测试 — STOCK > MARKET > UNKNOWN。

覆盖：
1. STOCK 优先级最高：命中个股事件时覆盖 MARKET/UNKNOWN，胜者身兼 source/confidence。
2. 宏观/大盘级文本 → MARKET（market_rule）。
3. 无任何命中 → UNKNOWN（默认）。
"""

from aistock_agent.services.event_scope_resolver import resolve_event_scope


def test_resolve_scope_prefers_stock_over_market(monkeypatch):
    import aistock_agent.services.event_scope_resolver as r
    monkeypatch.setattr(r, "detect_stock_event",
                        lambda *a, **k: {"event_scope": "STOCK", "event_scope_source": "eastmoney_rule", "event_scope_confidence": 0.95})
    scope, src, conf = resolve_event_scope("eastmoney", "美股隔夜大跌", "", {"symbol": "600519"})
    assert scope == "STOCK" and conf == 0.95


def test_resolve_scope_market_for_macro():
    scope, src, conf = resolve_event_scope("cls", "美联储按兵不动", "", {})
    assert scope == "MARKET" and src == "market_rule"


def test_resolve_scope_unknown_default():
    scope, _, _ = resolve_event_scope("cls", "某行业协会例行通知", "", {})
    assert scope == "UNKNOWN"
