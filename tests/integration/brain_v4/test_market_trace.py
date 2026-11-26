"""市场溯源试点集成测试（V4-08-P01 / Task 8）。

覆盖：
- 防污染：feature 源码不得导入旧 review worker / 生产快照构建器；
- adapter 只映射冻结快照的确定性事实与 provenance，不映射旧链判断；
- 同一 snapshot SHA 输入产生同一 evidence 集合；
- 流动性伪根因案例：>=3 竞争假设、结构化 Gap、一次定向补证、一次局部回改；
- 分数接近案例：完整议会路径（R0 独立、role-scoped PeerBrief、证据支撑的
  BeliefUpdate、少数意见、单写提交、有界终止）；
- 六因子投影只压缩已通过门禁的路径，且不新增 claim。

全部测试离线、确定性、不读系统时间、不触网、不调用真实 LLM。
"""

from __future__ import annotations

import json
from pathlib import Path

from aistock_agent.brain_v4.deliberation.roles import build_role_context
from aistock_agent.brain_v4.features.market_trace.adapter import (
    compute_snapshot_sha,
    load_frozen_snapshot,
    snapshot_to_evidence,
)
from aistock_agent.brain_v4.features.market_trace.profile import run_market_trace
from aistock_agent.brain_v4.features.market_trace.projection import (
    SIX_FACTOR_STAGES,
    project_six_factor,
)
from aistock_agent.brain_v4.graph.profiles import get_profile

_FIXTURE_DIR = (
    Path(__file__).resolve().parents[2] / "fixtures" / "brain_v4" / "market_trace"
)
_LIQUIDITY = _FIXTURE_DIR / "liquidity_pseudo_root.json"
_CLOSE_SCORE = _FIXTURE_DIR / "close_score_council.json"
_FEATURE_DIR = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "aistock_agent"
    / "brain_v4"
    / "features"
    / "market_trace"
)
_FORBIDDEN_TOKENS = (
    "agents.workers.review",
    "services.market_trace_snapshot",
    "build_market_trace_snapshot",
)


def _feature_sources() -> list[tuple[Path, str]]:
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(_FEATURE_DIR.rglob("*.py"))
    ]


def test_feature_sources_never_import_legacy_review_or_production_builder() -> None:
    sources = _feature_sources()
    assert sources, "feature 源码目录为空，无法执行防污染扫描"
    offenders = [
        f"{path.name}:{token}"
        for path, text in sources
        for token in _FORBIDDEN_TOKENS
        if token in text
    ]
    assert offenders == []


def test_adapter_maps_only_deterministic_provenance_not_legacy_narrative() -> None:
    case = load_frozen_snapshot(_LIQUIDITY)
    items = snapshot_to_evidence(
        case.snapshot, case.as_of, case.data_cutoff, case.snapshot.snapshot_id
    )

    assert items
    legacy_ids = set(case.legacy.output.get("evidence_ids", []))
    assert legacy_ids.isdisjoint({item.evidence_id for item in items})

    legacy_headline = str(case.legacy.output["headline"])
    dumped = json.dumps(
        [item.model_dump(mode="json") for item in items], ensure_ascii=False
    )
    assert legacy_headline not in dumped

    for item in items:
        assert "review" not in item.source.lower()
        assert all(
            not ref.startswith("legacy") for ref in item.supports_hypothesis_refs
        )
        assert all(
            not ref.startswith("legacy") for ref in item.contradicts_hypothesis_refs
        )
        assert item.as_of == case.as_of
        assert item.event_time <= case.data_cutoff
        assert item.published_at <= case.as_of
        assert item.snapshot_id == case.snapshot.snapshot_id


def test_same_snapshot_sha_yields_identical_evidence() -> None:
    case = load_frozen_snapshot(_LIQUIDITY)
    first_sha = compute_snapshot_sha(case.snapshot)
    second_sha = compute_snapshot_sha(case.snapshot)
    assert first_sha == second_sha
    assert len(first_sha) == 64

    first = snapshot_to_evidence(
        case.snapshot, case.as_of, case.data_cutoff, case.snapshot.snapshot_id
    )
    second = snapshot_to_evidence(
        case.snapshot, case.as_of, case.data_cutoff, case.snapshot.snapshot_id
    )
    assert [item.evidence_id for item in first] == [
        item.evidence_id for item in second
    ]
    assert [item.response_digest for item in first] == [
        item.response_digest for item in second
    ]


def test_liquidity_case_builds_competing_hypotheses_and_structured_gap() -> None:
    run = run_market_trace(load_frozen_snapshot(_LIQUIDITY))

    kinds = {hypothesis.hypothesis_id for hypothesis in run.hypotheses}
    assert {"hyp_liquidity", "hyp_style_divergence", "hyp_sector_rotation"} <= kinds
    assert len(run.hypotheses) >= 3

    resolved_gaps = [gap for gap in run.research_gaps if gap.status == "resolved"]
    assert resolved_gaps
    for gap in resolved_gaps:
        assert gap.gap_id
        assert gap.issue_id
        assert gap.gap_type in {"missing_evidence", "edge_support", "alternative"}
        assert gap.materiality in {"low", "medium", "high"}

    assert run.sufficiency_passed is True


def test_liquidity_case_adds_targeted_evidence_and_amplifier_revision() -> None:
    run = run_market_trace(load_frozen_snapshot(_LIQUIDITY))
    ledger_ids = {item.evidence_id for item in run.evidence_ledger.items}

    assert any(evidence_id.startswith("ev_supp_style") for evidence_id in ledger_ids)
    assert run.revision_events

    revision = run.revision_events[0]
    assert revision.action == "revise"
    assert "放大器" in revision.reason
    assert revision.triggering_evidence_ids
    assert set(revision.triggering_evidence_ids) <= ledger_ids

    liquidity_versions = [
        hypothesis
        for hypothesis in run.hypotheses
        if hypothesis.hypothesis_id == "hyp_liquidity"
    ]
    assert liquidity_versions
    assert any("放大器" in hypothesis.statement for hypothesis in liquidity_versions)
    assert all(hypothesis.version >= 2 for hypothesis in liquidity_versions)

    # 不得存在无证据的高置信因果边。
    for edge in run.causal_graph:
        if edge.confidence >= 0.5:
            assert edge.supporting_evidence_ids, edge.edge_id


def test_close_score_case_runs_bounded_council_with_independence_and_minority() -> None:
    run = run_market_trace(load_frozen_snapshot(_CLOSE_SCORE))

    assert run.collaboration_decision.mode == "ANALYST_COUNCIL"
    assert run.council_decision is not None
    state = run.deliberation_state
    assert state is not None

    # R0 独立：首轮没有任何同伴立场或摘要可见。
    r0 = build_role_context(round_no=0, role="lead_thesis", state=state)
    assert r0.public_positions == []
    assert r0.peer_briefs == []
    assert any(
        paper.position_id.startswith("pos_r00") for paper in run.submissions
    )

    # R1 只有 role-scoped PeerBrief。
    r1 = build_role_context(round_no=1, role="lead_thesis", state=state)
    assert r1.peer_briefs
    assert all(brief.target_role == "lead_thesis" for brief in r1.peer_briefs)
    assert all(
        paper.analyst_role != "lead_thesis" for paper in r1.public_positions
    )

    # 非 hold 的 BeliefUpdate 必须有证据支撑，且证据在冻结账本内。
    ledger_ids = {item.evidence_id for item in run.evidence_ledger.items}
    changed = [
        belief
        for belief in run.belief_updates
        if belief.deliberation_round >= 1 and belief.action != "hold"
    ]
    assert changed
    for belief in changed:
        assert belief.evidence_ids
        assert set(belief.evidence_ids) <= ledger_ids

    # 少数意见保留。
    assert run.council_decision.minority_opinions

    # 单写提交：只产生一条回改事件，且无并发冲突。
    assert len(run.revision_events) == 1
    assert state.get("conflicts", []) == []

    # 有界终止：议会轮次不超过 profile 上限，研判显式记录停止原因。
    max_round = max(
        ledger.deliberation_round
        for ledger in state.get("deliberation_round_ledgers", [])
    )
    assert max_round <= get_profile("market_trace").max_deliberation_rounds
    assert run.judgment.stop_reason


def test_projection_compresses_only_gated_paths_without_new_claims() -> None:
    run = run_market_trace(load_frozen_snapshot(_LIQUIDITY))
    ledger_ids = {item.evidence_id for item in run.evidence_ledger.items}
    edge_ids = {edge.edge_id for edge in run.causal_graph}

    chain = project_six_factor(
        hypotheses=run.hypotheses,
        causal_edges=run.causal_graph,
        evidence_ids=ledger_ids,
        snapshot_id=run.snapshot_id,
        gate_passed=run.sufficiency_passed,
    )

    assert [step.stage for step in chain.steps] == list(SIX_FACTOR_STAGES)
    mechanisms = {edge.mechanism for edge in run.causal_graph}
    for step in chain.steps:
        assert set(step.evidence_ids) <= ledger_ids
        assert set(step.edge_ids) <= edge_ids
        if step.status == "supported":
            assert step.evidence_ids
            assert step.statement in mechanisms
        if step.edge_ids:
            assert step.statement in mechanisms

    assert chain.hypothesis_only_edge_ids
    hypothesis_only = {
        edge.edge_id
        for edge in run.causal_graph
        if not edge.supporting_evidence_ids or edge.edge_type == "hypothesis"
    }
    assert set(chain.hypothesis_only_edge_ids) == hypothesis_only


def test_judgment_references_only_frozen_ledger_evidence() -> None:
    run = run_market_trace(load_frozen_snapshot(_LIQUIDITY))
    ledger_ids = {item.evidence_id for item in run.evidence_ledger.items}

    assert set(run.judgment.evidence_ids) <= ledger_ids
    assert run.judgment.as_of == run.as_of
    assert run.judgment.data_cutoff == run.data_cutoff
    assert run.judgment.snapshot_id == run.snapshot_id
