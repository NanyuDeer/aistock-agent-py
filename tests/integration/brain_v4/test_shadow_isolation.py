"""影子隔离集成测试（Task 7 Step 1）。

验收口径：
- 影子信封不进任何正式查询投影；
- 快照 SHA 不一致不得形成 comparison；
- 旧链与 V4 必须是不同 run；
- 旧链证据 ID 不得被当作 V4 证据。
"""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from brain_v4_evals.ab.run_paired import PairingError, SnapshotMismatchError, build_envelope
from brain_v4_evals.contracts import select_active

AS_OF = datetime(2026, 11, 26, 15, 30, tzinfo=UTC)
CUTOFF = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)


def _run(run_id: str, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": run_id,
        "feature_id": "market_trace",
        "profile": "market_trace",
        "subject_type": "market",
        "subject_code": "000001-SH",
        "snapshot_id": "snap_shared",
        "snapshot_sha": "b" * 64,
        "as_of": AS_OF,
        "data_cutoff": CUTOFF,
        "model_version": "m1",
        "config_version": "c1",
        "method_version": "v4.0",
        "output": {"headline": "结论"},
        "evidence_ids": [f"{run_id}-ev-1"],
        "counter_evidence_ids": [],
        "claim_ids": [f"{run_id}-claim-1"],
        "hypotheses": [],
        "revisions": [],
        "unresolved_questions": [],
        "latency_ms": 800,
        "token_usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        "cost": 0.01,
        "quality_metrics": {},
        "reviewer_score": None,
    }
    base.update(overrides)
    return base


def test_shadow_envelope_never_enters_active_projection() -> None:
    envelope = build_envelope(_run("legacy-1"), _run("v4-1"), blind_seed=2)
    assert envelope.is_shadow is True
    assert select_active([envelope]) == []


def test_envelope_round_trip_stays_shadow() -> None:
    envelope = build_envelope(_run("legacy-1"), _run("v4-1"), blind_seed=2)
    restored = envelope.model_validate(envelope.model_dump(mode="json"))
    assert restored.is_shadow is True
    assert select_active([restored]) == []


def test_mismatched_snapshot_sha_cannot_form_comparison() -> None:
    legacy = _run("legacy-1", snapshot_sha="c" * 64)
    v4 = _run("v4-1", snapshot_sha="d" * 64)
    with pytest.raises(SnapshotMismatchError):
        build_envelope(legacy, v4, blind_seed=1)


def test_mismatched_snapshot_id_cannot_form_comparison() -> None:
    legacy = _run("legacy-1", snapshot_id="snap_a")
    v4 = _run("v4-1", snapshot_id="snap_b")
    with pytest.raises(SnapshotMismatchError):
        build_envelope(legacy, v4, blind_seed=1)


def test_same_run_id_cannot_pair() -> None:
    with pytest.raises(PairingError):
        build_envelope(_run("same"), _run("same"), blind_seed=1)


def test_legacy_evidence_id_cannot_flow_into_v4_evidence() -> None:
    legacy = _run("legacy-1", evidence_ids=["shared-ev"], output={"evidence_ids": ["shared-ev"]})
    v4 = _run("v4-1", evidence_ids=["shared-ev"])
    with pytest.raises(ValidationError):
        build_envelope(legacy, v4, blind_seed=1)


def test_comparison_id_is_deterministic() -> None:
    first = build_envelope(_run("legacy-1"), _run("v4-1"), blind_seed=4)
    second = build_envelope(_run("legacy-1"), _run("v4-1"), blind_seed=99)
    assert first.comparison_id == second.comparison_id
    assert first.comparison_id.startswith("cmp_")
