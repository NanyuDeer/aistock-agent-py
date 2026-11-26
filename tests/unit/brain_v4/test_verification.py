"""到期验证、错误标签、方法候选与议会质量指标（V4-10-P01 / Task 10）。

覆盖：
- 验证器 append-only，绝不修改原 judgment；
- 方向正确但主因错误 -> `cause_wrong` 且不含 `direction_wrong`；
- 高置信 miss 追加 `overconfidence`；
- 时间泄漏、浅层证据、无支撑边、insufficient 漏判各自可取到标签；
- 方法候选永远 draft、永不写生产，必须经回放 + 配对 A/B 才能推进状态；
- 议会质量指标：轮次、belief 变化、少数意见保留率、议会有效性、重复工具调用、
  时间泄漏。

全部测试离线、确定性、不读系统时间、不触网。
"""

from __future__ import annotations

import datetime
import importlib

import pytest

from aistock_agent.brain_v4.contracts.deliberation import BeliefUpdate
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem
from aistock_agent.brain_v4.contracts.judgment import V4Judgment
from aistock_agent.brain_v4.learning import candidates
from aistock_agent.brain_v4.learning.candidates import (
    CandidateTransitionError,
    MethodCandidate,
    create_candidate,
    record_ab,
    record_replay,
    reject_candidate,
)
from aistock_agent.brain_v4.learning.deliberation_metrics import (
    DeliberationInputs,
    measure_deliberation,
)
from aistock_agent.brain_v4.verification.error_tags import classify_error
from aistock_agent.brain_v4.verification.verifier import (
    ObservedOutcome,
    Verifier,
    verify_judgment,
)

_TZ = datetime.timezone(datetime.timedelta(hours=8))
_AS_OF = datetime.datetime(2026, 11, 26, 15, 30, tzinfo=_TZ)
_CUTOFF = datetime.datetime(2026, 11, 26, 15, 0, tzinfo=_TZ)


def _judgment(
    *,
    decision: str = "accept",
    status: str = "ok",
    confidence: float = 0.72,
    judgment_id: str = "jdg_run_market_trace_liquidity_01",
) -> V4Judgment:
    return V4Judgment(
        judgment_id=judgment_id,
        run_id="run_market_trace_liquidity",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        horizon="1d",
        as_of=_AS_OF,
        data_cutoff=_CUTOFF,
        snapshot_id="trace-20261126-test",
        decision=decision,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        stop_reason="sufficient",
        selected_hypothesis_refs=["hyp_style_divergence:v1"],
        rejected_hypothesis_refs=["hyp_liquidity:v2"],
        confidence=confidence,
        config_version="cfg_market_trace_v1",
        model_version="v4-market-trace-1",
        prompt_version="prompt_market_trace_v1",
        method_version="method_v4.0",
        created_at=_AS_OF,
    )


def _observed(direction: str, cause: str | None) -> ObservedOutcome:
    return ObservedOutcome(
        direction=direction,
        primary_cause=cause,
        observed_at=datetime.datetime(2026, 11, 27, 15, 30, tzinfo=_TZ),
    )


def test_verifier_never_mutates_original_judgment() -> None:
    judgment = _judgment()
    before = judgment.model_dump()

    verifier = Verifier()
    record = verifier.verify(
        judgment,
        expected_direction="down",
        expected_primary_cause="style_divergence",
        observed=_observed("down", "style_divergence"),
    )

    assert judgment.model_dump() == before
    assert record.judgment_id == judgment.judgment_id
    assert record.verdict == "correct"
    assert record.error_tags == []


def test_direction_right_cause_wrong_only_flags_cause() -> None:
    judgment = _judgment()
    record = verify_judgment(
        judgment,
        expected_direction="down",
        expected_primary_cause="style_divergence",
        observed=_observed("down", "liquidity_contraction"),
    )

    assert record.direction_match is True
    assert record.cause_match is False
    assert "cause_wrong" in record.error_tags
    assert "direction_wrong" not in record.error_tags
    assert record.verdict == "partially_correct"


def test_high_confidence_miss_adds_overconfidence() -> None:
    judgment = _judgment(confidence=0.9)
    record = verify_judgment(
        judgment,
        expected_direction="down",
        expected_primary_cause="style_divergence",
        observed=_observed("up", "policy_shift"),
        high_confidence_threshold=0.7,
    )

    assert record.direction_match is False
    assert "direction_wrong" in record.error_tags
    assert "overconfidence" in record.error_tags
    assert record.verdict == "wrong"


def test_low_confidence_miss_has_no_overconfidence() -> None:
    tags = classify_error(
        direction_match=False,
        cause_match=False,
        confidence=0.4,
        high_confidence_threshold=0.7,
    )
    assert "direction_wrong" in tags
    assert "overconfidence" not in tags


def test_all_error_categories_reachable() -> None:
    tags = classify_error(
        direction_match=False,
        cause_match=False,
        confidence=0.8,
        high_confidence_threshold=0.7,
        time_leakage=True,
        evidence_depth=1,
        min_evidence_depth=2,
        unsupported_edge_ratio=0.5,
        max_unsupported_edge_ratio=0.3,
        insufficient_missed=True,
    )
    assert set(tags) == {
        "direction_wrong",
        "overconfidence",
        "time_leakage",
        "shallow_evidence",
        "unsupported_edge",
        "insufficient_missed",
    }


def test_verifier_appends_records_without_rewriting_history() -> None:
    verifier = Verifier()
    first = verifier.verify(
        _judgment(),
        expected_direction="down",
        expected_primary_cause="style_divergence",
        observed=_observed("down", "style_divergence"),
    )
    snapshot = first.model_dump()
    second = verifier.verify(
        _judgment(judgment_id="jdg_second"),
        expected_direction="down",
        expected_primary_cause="style_divergence",
        observed=_observed("up", "policy_shift"),
    )

    assert len(verifier.records) == 2
    assert verifier.records[0].model_dump() == snapshot
    assert verifier.records[1].judgment_id == "jdg_second"
    assert second.verification_id != first.verification_id


def test_insufficient_judgment_missed_is_flagged() -> None:
    record = verify_judgment(
        _judgment(decision="insufficient", status="insufficient"),
        expected_direction="down",
        expected_primary_cause="style_divergence",
        observed=_observed("down", "style_divergence"),
    )
    assert "insufficient_missed" in record.error_tags


def test_candidate_is_always_draft_and_never_production() -> None:
    candidate = create_candidate(
        candidate_id="cand_1",
        feature_id="market_trace",
        method_version="method_v4.1",
        change_summary="重排风格与流动性证据权重",
        rationale="回放显示主因稳健性提升",
        source_run_ids=["run_market_trace_liquidity"],
    )
    assert isinstance(candidate, MethodCandidate)
    assert candidate.status == "draft"
    assert candidate.draft is True
    assert candidate.applied_to_production is False


def test_candidate_transitions_are_gated_and_never_touch_production() -> None:
    candidate = create_candidate(
        candidate_id="cand_1",
        feature_id="market_trace",
        method_version="method_v4.1",
        change_summary="重排证据权重",
        rationale="回放稳健",
        source_run_ids=["run_1"],
    )

    with pytest.raises(CandidateTransitionError):
        record_replay(candidate, replay_case_ids=[])
    with pytest.raises(CandidateTransitionError):
        record_ab(candidate, ab_comparison_ids=["cmp_1"])

    replayed = record_replay(
        candidate, replay_case_ids=["case_a", "case_b"]
    )
    assert replayed.status == "replay_passed"
    assert replayed.draft is True
    assert replayed.applied_to_production is False

    with pytest.raises(CandidateTransitionError):
        record_ab(replayed, ab_comparison_ids=[])

    ab_passed = record_ab(replayed, ab_comparison_ids=["cmp_1"])
    assert ab_passed.status == "ab_passed"
    assert ab_passed.applied_to_production is False

    rejected = reject_candidate(ab_passed, reason="人工复核否决")
    assert rejected.status == "rejected"
    assert rejected.applied_to_production is False

    # 不存在直接写生产方法的 API。
    public = {name for name in dir(candidates) if not name.startswith("_")}
    assert not public & {
        "apply_to_production",
        "write_production",
        "promote_to_production",
        "install_method",
    }


def test_candidate_rejects_invalid_direct_state() -> None:
    import pydantic

    with pytest.raises(pydantic.ValidationError):
        MethodCandidate(
            candidate_id="cand_bad",
            feature_id="market_trace",
            method_version="method_v4.1",
            change_summary="x",
            rationale="y",
            status="ab_passed",
        )


def _belief(
    belief_update_id: str,
    *,
    action: str,
    round_no: int,
) -> BeliefUpdate:
    return BeliefUpdate(
        belief_update_id=belief_update_id,
        issue_id="issue_1",
        analyst_role="structural_analyst",
        case_version=1,
        deliberation_round=round_no,
        prior_hypothesis_ref="hyp_liquidity:v1",
        prior_confidence=0.56,
        action=action,  # type: ignore[arg-type]
        posterior_hypothesis_refs=["hyp_style_divergence:v1"],
        posterior_confidence=[0.6],
        reason_summary=f"{action} after cross exam",
    )


def _evidence(
    evidence_id: str,
    *,
    source_id: str,
    event_time: datetime.datetime,
    published_at: datetime.datetime,
    digest: str,
) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source="tushare:daily",
        source_type="market_fact",
        event_time=event_time,
        published_at=published_at,
        fetched_at=_AS_OF,
        as_of=_AS_OF,
        evidence_kind="direct",
        data_quality="high",
        time_confidence="high",
        tool="frozen_snapshot",
        tool_params={"source_id": source_id},
        response_digest=digest,
        snapshot_id="trace-20261126-test",
    )


def test_deliberation_metrics_cover_rounds_beliefs_minority_and_leakage() -> None:
    inputs = DeliberationInputs(
        collaboration_mode="ANALYST_COUNCIL",
        stop_reason="judged",
        judgment_status="ok",
        belief_updates=[
            _belief("bu_1", action="hold", round_no=0),
            _belief("bu_2", action="strengthen", round_no=1),
        ],
        minority_opinions_raised=["板块轮动不可忽略", "北向口径偏差"],
        minority_opinions_retained=["板块轮动不可忽略"],
        evidence_items=[
            _evidence(
                "ev_1",
                source_id="STYLE_LARGE",
                event_time=_CUTOFF,
                published_at=_AS_OF,
                digest="d1",
            ),
            # 与 ev_1 同 tool + 同参数 -> 重复工具调用。
            _evidence(
                "ev_2",
                source_id="STYLE_LARGE",
                event_time=_CUTOFF,
                published_at=_AS_OF,
                digest="d1",
            ),
            # event_time 晚于 cutoff -> 时间泄漏。
            _evidence(
                "ev_3",
                source_id="SECTOR_GROWTH",
                event_time=_AS_OF + datetime.timedelta(hours=1),
                published_at=_AS_OF,
                digest="d3",
            ),
        ],
        as_of=_AS_OF,
        data_cutoff=_CUTOFF,
    )

    metrics = measure_deliberation(inputs)

    assert metrics.deliberation_rounds == 2
    assert metrics.belief_update_count == 2
    assert metrics.belief_change_count == 1
    assert metrics.minority_raised_count == 2
    assert metrics.minority_retained_count == 1
    assert metrics.minority_retention_rate == 0.5
    assert metrics.council_mode_engaged is True
    assert metrics.council_effective is True
    assert metrics.duplicate_tool_call_count == 1
    assert metrics.time_leakage_count == 1


def test_deliberation_metrics_non_council_and_no_minority() -> None:
    inputs = DeliberationInputs(
        collaboration_mode="SOLO_DEEP",
        stop_reason="sufficient",
        judgment_status="ok",
        belief_updates=[],
        minority_opinions_raised=[],
        minority_opinions_retained=[],
        evidence_items=[],
        as_of=_AS_OF,
        data_cutoff=_CUTOFF,
    )
    metrics = measure_deliberation(inputs)
    assert metrics.council_mode_engaged is False
    assert metrics.council_effective is False
    assert metrics.minority_retention_rate == 0.0
    assert metrics.deliberation_rounds == 0


def test_verification_module_is_importable_without_side_effects() -> None:
    module = importlib.import_module("aistock_agent.brain_v4.verification")
    assert module is not None
