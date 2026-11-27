"""分析师引用的证据 ID 必须对齐冻结 ledger（截断/臆造不得让整份研判失败）。"""

from __future__ import annotations

from aistock_agent.brain_v4.runtime.briefing_live import _resolve_evidence_refs

LEDGER = {
    "ev_brief_0a0a8f513e3b_prev_index_000001_sh",
    "ev_brief_329d74fa2137_breadth_all",
}


def test_exact_id_is_kept() -> None:
    assert _resolve_evidence_refs(
        ["ev_brief_329d74fa2137_breadth_all"], LEDGER
    ) == ["ev_brief_329d74fa2137_breadth_all"]


def test_unique_prefix_is_expanded() -> None:
    # 实测模型会把 ID 截成 `ev_brief_<digest>`。
    assert _resolve_evidence_refs(["ev_brief_0a0a8f513e3b"], LEDGER) == [
        "ev_brief_0a0a8f513e3b_prev_index_000001_sh"
    ]


def test_unknown_or_ambiguous_refs_are_dropped() -> None:
    assert _resolve_evidence_refs(["ev_unknown", "ev_brief_"], LEDGER) == []


def test_non_string_refs_are_ignored() -> None:
    assert _resolve_evidence_refs(["", "ev_brief_329d74fa2137_breadth_all"], LEDGER) == [
        "ev_brief_329d74fa2137_breadth_all"
    ]
