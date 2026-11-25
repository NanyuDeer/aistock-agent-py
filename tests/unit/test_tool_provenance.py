"""工具信封：溯源元数据必须能无损落到 JudgmentEvidence。"""

from aistock_agent.tools import provenance
from aistock_agent.tools.provenance import (
    degraded_envelope,
    envelope_to_evidence,
    get_domain_tools,
    ok_envelope,
)


class _FakeTool:
    def __init__(self, name: str) -> None:
        self.name = name


def test_ok_envelope_maps_provenance_to_evidence():
    envelope = ok_envelope(
        {"close": 3200.0},
        source="internal:index_kline",
        source_type="market_data",
        published_at="2026-11-25",
        quality="high",
    )
    evidence = envelope_to_evidence(
        envelope,
        evidence_id="ev_idx_20261125",
        kind="market",
        subject="上证指数",
        claim="上证指数收 3200.00",
        weight=1.0,
    )
    assert evidence.source == "internal:index_kline"
    assert evidence.quality == "high"
    assert evidence.published_at == "2026-11-25"
    assert evidence.degraded is False
    assert "T" in evidence.fetched_at
    assert evidence.fetched_at.endswith("+00:00")


def test_degraded_envelope_forces_low_quality():
    envelope = degraded_envelope(
        source="internal:index_kline", source_type="market_data", error="index_kline_empty"
    )
    evidence = envelope_to_evidence(
        envelope,
        evidence_id="ev_idx_missing",
        kind="market",
        subject="上证指数",
        claim="指数数据缺失",
    )
    assert evidence.quality == "low"
    assert evidence.degraded is True


def test_get_domain_tools_maps_categories_and_dedupes(monkeypatch):
    # market 域聚合 morning / trend_score / general 三个 category，
    # 三者都返回同名 get_quote —— 用于真正覆盖按 name 去重分支。
    category_tools = {
        "morning": ["get_quote", "get_index_kline"],
        "trend_score": ["get_quote", "get_trend_score"],
        "general": ["get_quote", "get_capital_flow"],
    }
    monkeypatch.setattr(
        provenance,
        "_get_tools",
        lambda category: [_FakeTool(name) for name in category_tools.get(category, [])],
    )
    tools = get_domain_tools("market")
    names = [tool.name for tool in tools]
    # 顺序遵循 AGENT_DOMAIN 遍历顺序（morning → trend_score → general）
    assert names == ["get_quote", "get_index_kline", "get_trend_score", "get_capital_flow"]
    # 关键断言：重复工具只保留首次出现的一次（无去重时为 3 次）
    assert names.count("get_quote") == 1
    assert get_domain_tools("unknown") == []
