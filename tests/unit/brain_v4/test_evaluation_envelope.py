"""通用评测信封与静态评测台契约测试（Task 7 Step 1）。

覆盖：
- 未知字段 / `is_shadow=False` 必须被拒绝；
- 旧链输出证据 ID 不得被复制进 V4 evidence；
- 同一 seed 的盲标确定性；
- 静态 HTML 报告可离线打开、含旧链/V4 双栏与快照元数据。
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from brain_v4_evals.ab.run_paired import build_envelope
from brain_v4_evals.contracts import EvaluationEnvelope
from brain_v4_evals.reporting.generate import generate_report

AS_OF = datetime(2026, 11, 26, 15, 30, tzinfo=UTC)
CUTOFF = datetime(2026, 11, 26, 15, 0, tzinfo=UTC)


def _run(run_id: str, *, output: dict[str, object] | None = None, **overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "run_id": run_id,
        "feature_id": "market_trace",
        "profile": "market_trace",
        "subject_type": "market",
        "subject_code": "000001-SH",
        "snapshot_id": "snap_abc123",
        "snapshot_sha": "a" * 64,
        "as_of": AS_OF,
        "data_cutoff": CUTOFF,
        "model_version": "m1",
        "config_version": "c1",
        "method_version": "v4.0",
        "output": output or {"headline": "旧链结论" if run_id.startswith("legacy") else "V4 结论"},
        "evidence_ids": [f"{run_id}-ev-1"],
        "counter_evidence_ids": [],
        "claim_ids": [f"{run_id}-claim-1"],
        "hypotheses": [
            {
                "hypothesis_ref": "hyp_a:v1",
                "statement": "流动性是放大器",
                "status": "supported",
                "confidence": 0.7,
            }
        ],
        "revisions": [],
        "unresolved_questions": ["缺口一"],
        "latency_ms": 1200,
        "token_usage": {
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
        },
        "cost": 0.02,
        "quality_metrics": {"evidence_coverage": 0.8},
        "reviewer_score": None,
    }
    base.update(overrides)
    return base


def _envelope(**overrides: object) -> EvaluationEnvelope:
    legacy = _run("legacy-run-1", output={"headline": "旧链结论"})
    v4 = _run("v4-run-1", output={"headline": "V4 结论"})
    envelope = build_envelope(legacy, v4, blind_seed=7)
    if not overrides:
        return envelope
    payload = envelope.model_dump(mode="json")
    payload.update(overrides)
    return EvaluationEnvelope.model_validate(payload)


def test_unknown_field_is_rejected() -> None:
    payload = _envelope().model_dump(mode="json")
    payload["unexpected_field"] = 1
    with pytest.raises(ValidationError):
        EvaluationEnvelope.model_validate(payload)


def test_shadow_flag_must_be_true() -> None:
    with pytest.raises(ValidationError):
        _envelope(is_shadow=False)


def test_legacy_evidence_id_cannot_be_copied_into_v4_evidence() -> None:
    payload = _envelope().model_dump(mode="json")
    payload["evidence_summary"] = {
        "summary": "V4 证据",
        "evidence_ids": ["legacy-run-1-ev-1"],
        "counter_evidence_ids": [],
        "claim_ids": [],
    }
    payload["legacy_output"] = {"evidence_ids": ["legacy-run-1-ev-1"]}
    with pytest.raises(ValidationError):
        EvaluationEnvelope.model_validate(payload)


def test_blind_label_is_deterministic_for_same_seed() -> None:
    legacy = _run("legacy-run-1")
    v4 = _run("v4-run-1")
    first = build_envelope(legacy, v4, blind_seed=11)
    second = build_envelope(legacy, v4, blind_seed=11)
    assert first.blind_label == second.blind_label
    assert first.blind_label in {"A", "B"}

    labels = {
        build_envelope(legacy, v4, blind_seed=seed).blind_label for seed in range(40)
    }
    assert labels == {"A", "B"}


def test_generate_report_writes_offline_dual_column_html(tmp_path: Path) -> None:
    envelope = build_envelope(_run("legacy-run-1"), _run("v4-run-1"), blind_seed=3)
    paths = generate_report([envelope], tmp_path / "report")

    assert paths.index.is_file()
    page = paths.pages["market_trace"]
    html = page.read_text(encoding="utf-8")
    assert "Legacy" in html
    assert "V4" in html
    assert envelope.snapshot_sha in html
    assert envelope.snapshot_id in html
    assert "旧链结论" in html
    assert "V4 结论" in html
    assert "假设" in html
    assert "回改" in html
    assert "未解决问题" in html

    data_file = paths.data[envelope.comparison_id]
    payload = json.loads(data_file.read_text(encoding="utf-8"))
    assert payload["is_shadow"] is True
    assert payload["comparison_id"] == envelope.comparison_id

    static_dir = (tmp_path / "report") / "static"
    index_js = (static_dir / "index.js").read_text(encoding="utf-8")
    assert (static_dir / "styles.css").is_file()
    # 离线可打开：不得发起任何实时请求。
    assert "fetch(" not in index_js
    assert "XMLHttpRequest" not in index_js


def test_generate_report_page_references_relative_static(tmp_path: Path) -> None:
    envelope = build_envelope(_run("legacy-run-1"), _run("v4-run-1"), blind_seed=5)
    paths = generate_report([envelope], tmp_path / "report")
    html = paths.pages["market_trace"].read_text(encoding="utf-8")
    assert "../static/styles.css" in html
    assert "../static/index.js" in html
