"""旧链 / V4 配对运行与确定性盲标。

约束（Task 7 Step 3）：
- 只有 `snapshot_id` 与 `snapshot_sha` 完全一致才允许形成 comparison；
- 旧链与 V4 必须是不同的 run；
- 盲标只由 `(blind_seed, comparison_id)` 决定，同 seed 同标签，可复现；
- 旧链输出单独存放，不得注入 V4 evidence（由 `EvaluationEnvelope` 校验）。
"""

import hashlib
from collections.abc import Mapping
from typing import Any

from ..contracts import (
    BlindLabel,
    EvalHypothesis,
    EvalRevision,
    EvalSubject,
    EvaluationEnvelope,
    EvidenceSummary,
    TokenUsage,
)


class PairingError(ValueError):
    """配对输入缺失或结构非法。"""


class SnapshotMismatchError(PairingError):
    """旧链与 V4 不在同一冻结快照上，禁止形成 comparison。"""


def _field(run: Mapping[str, Any], key: str, label: str) -> Any:
    if key not in run:
        raise PairingError(f"{label} 缺少必需字段 {key!r}")
    return run[key]


def _as_dict(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise PairingError(f"{label} 必须是对象")
    return {str(key): item for key, item in value.items()}


def _as_list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise PairingError(f"{label} 必须是数组")
    return list(value)


def _comparison_id(feature_id: str, snapshot_sha: str) -> str:
    digest = hashlib.sha256(f"{feature_id}|{snapshot_sha}".encode()).hexdigest()
    return f"cmp_{digest[:16]}"


def blind_label(blind_seed: int, comparison_id: str) -> BlindLabel:
    """确定性盲标：同一 seed + comparison 必然得到同一 A/B 标签。"""
    digest = hashlib.sha256(f"{blind_seed}:{comparison_id}".encode()).hexdigest()
    return "A" if int(digest[:2], 16) % 2 == 0 else "B"


def build_envelope(
    legacy_run: Mapping[str, Any],
    v4_run: Mapping[str, Any],
    blind_seed: int,
) -> EvaluationEnvelope:
    """把同快照的旧链与 V4 运行配对为不可变评测信封。"""
    legacy_run_id = str(_field(legacy_run, "run_id", "legacy_run"))
    v4_run_id = str(_field(v4_run, "run_id", "v4_run"))
    if legacy_run_id == v4_run_id:
        raise PairingError("legacy_run 与 v4_run 必须使用不同 run_id")

    feature_id = str(_field(v4_run, "feature_id", "v4_run"))
    legacy_sha = str(_field(legacy_run, "snapshot_sha", "legacy_run"))
    v4_sha = str(_field(v4_run, "snapshot_sha", "v4_run"))
    legacy_snapshot_id = str(_field(legacy_run, "snapshot_id", "legacy_run"))
    v4_snapshot_id = str(_field(v4_run, "snapshot_id", "v4_run"))
    if not legacy_sha or not v4_sha or legacy_sha != v4_sha:
        raise SnapshotMismatchError(
            f"snapshot_sha 不一致，不能形成 comparison: {legacy_sha!r} != {v4_sha!r}"
        )
    if legacy_snapshot_id != v4_snapshot_id:
        raise SnapshotMismatchError(
            f"snapshot_id 不一致，不能形成 comparison: {legacy_snapshot_id!r} != {v4_snapshot_id!r}"
        )

    comparison_id = _comparison_id(feature_id, v4_sha)
    subject = EvalSubject(
        subject_type=str(_field(v4_run, "subject_type", "v4_run")),
        subject_code=str(_field(v4_run, "subject_code", "v4_run")),
    )
    evidence_summary = EvidenceSummary(
        summary=str(v4_run.get("evidence_summary", "")),
        evidence_ids=[str(item) for item in _as_list(v4_run.get("evidence_ids", []), "evidence_ids")],
        counter_evidence_ids=[
            str(item)
            for item in _as_list(v4_run.get("counter_evidence_ids", []), "counter_evidence_ids")
        ],
        claim_ids=[str(item) for item in _as_list(v4_run.get("claim_ids", []), "claim_ids")],
    )

    hypotheses = [
        EvalHypothesis.model_validate(item)
        for item in _as_list(v4_run.get("hypotheses", []), "hypotheses")
    ]
    revisions = [
        EvalRevision.model_validate(item)
        for item in _as_list(v4_run.get("revisions", []), "revisions")
    ]

    raw_score = v4_run.get("reviewer_score")
    reviewer_score = None if raw_score is None else float(raw_score)
    quality_metrics = {
        str(key): float(value)
        for key, value in _as_dict(v4_run.get("quality_metrics", {}), "quality_metrics").items()
    }

    return EvaluationEnvelope(
        feature_id=feature_id,
        profile=str(_field(v4_run, "profile", "v4_run")),
        subject=subject,
        comparison_id=comparison_id,
        legacy_run_id=legacy_run_id,
        v4_run_id=v4_run_id,
        snapshot_id=v4_snapshot_id,
        snapshot_sha=v4_sha,
        as_of=_field(v4_run, "as_of", "v4_run"),
        data_cutoff=_field(v4_run, "data_cutoff", "v4_run"),
        model_version=str(_field(v4_run, "model_version", "v4_run")),
        config_version=str(_field(v4_run, "config_version", "v4_run")),
        method_version=str(_field(v4_run, "method_version", "v4_run")),
        legacy_output=_as_dict(legacy_run.get("output", {}), "legacy_run.output"),
        v4_output=_as_dict(v4_run.get("output", {}), "v4_run.output"),
        evidence_summary=evidence_summary,
        hypotheses=hypotheses,
        revisions=revisions,
        unresolved_questions=[
            str(item)
            for item in _as_list(v4_run.get("unresolved_questions", []), "unresolved_questions")
        ],
        latency_ms=int(_field(v4_run, "latency_ms", "v4_run")),
        token_usage=TokenUsage.model_validate(_field(v4_run, "token_usage", "v4_run")),
        cost=float(_field(v4_run, "cost", "v4_run")),
        quality_metrics=quality_metrics,
        reviewer_score=reviewer_score,
        blind_label=blind_label(blind_seed, comparison_id),
        is_shadow=True,
    )


__all__ = [
    "PairingError",
    "SnapshotMismatchError",
    "blind_label",
    "build_envelope",
]
