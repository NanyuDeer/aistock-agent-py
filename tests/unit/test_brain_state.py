"""BrainState 通道与 reducer —— 同时守住"未声明键被静默丢弃"这个已知坑。"""

import pytest

from aistock_agent.graph.brain.state import (
    append,
    append_dedupe,
    latest_by_version,
    latest_committed,
    set_union,
)
from aistock_agent.schemas.judgment import JudgmentEvidence


def _ev(evidence_id: str, claim: str = "x") -> JudgmentEvidence:
    return JudgmentEvidence(
        evidence_id=evidence_id,
        kind="market",
        subject="上证指数",
        claim=claim,
        source="internal:index_kline",
        source_type="market_data",
        published_at="2026-11-25",
        fetched_at="2026-11-25T07:00:00+00:00",
    )


def test_append_dedupe_keeps_first():
    reducer = append_dedupe("evidence_id")
    merged = reducer([_ev("ev_1", "first")], [_ev("ev_1", "second"), _ev("ev_2")])
    assert [item.evidence_id for item in merged] == ["ev_1", "ev_2"]
    assert merged[0].claim == "first"


def test_append_dedupe_rejects_element_missing_key():
    """缺 id 的元素不能被静默折叠成同一条：必须报错并指出缺失的 key 名。"""
    reducer = append_dedupe("evidence_id")
    with pytest.raises(ValueError, match="missing key 'evidence_id'"):
        reducer([{"evidence_id": "ev_1"}], [{"claim": "missing id"}])


def test_append_keeps_duplicates_for_audit():
    merged = append([{"node": "a"}], [{"node": "a"}])
    assert len(merged) == 2


def test_set_union_sorted_and_deduped():
    assert set_union(["home"], ["chat", "home"]) == ["chat", "home"]


def test_latest_committed_ignores_none():
    marker = object()
    assert latest_committed(marker, None) is marker
    assert latest_committed(marker, "new") == "new"


def test_latest_by_version_overrides_same_key():
    assert latest_by_version({"short": {"v": 1}}, {"short": {"v": 2}}) == {"short": {"v": 2}}


@pytest.mark.asyncio
async def test_channels_survive_graph_roundtrip():
    """真图跑一遍：证据/出口/追踪都必须留在终态（防未声明键被静默丢弃）。"""
    from langgraph.graph import END, START, StateGraph

    from aistock_agent.graph.brain.state import BrainState

    graph = StateGraph(BrainState)
    graph.add_node(
        "first",
        lambda state: {"evidence": [_ev("ev_1")], "render_targets": ["home"], "trace": [{"node": "first"}]},
    )
    graph.add_node(
        "second",
        lambda state: {"evidence": [_ev("ev_1"), _ev("ev_2")], "render_targets": ["chat"]},
    )
    graph.add_edge(START, "first")
    graph.add_edge("first", "second")
    graph.add_edge("second", END)

    out = await graph.compile().ainvoke({"subject_code": "000001", "as_of": "2026-11-25"})
    assert [item.evidence_id for item in out["evidence"]] == ["ev_1", "ev_2"]
    assert out["render_targets"] == ["chat", "home"]
    assert len(out["trace"]) == 1
