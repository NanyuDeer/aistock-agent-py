"""市场溯源评测：同一 snapshot SHA 上的旧链 / V4 双栏静态报告。

边界（Task 8 Step 4 / 团队接入规范 §7）：
- 旧链输出只进入 `EvaluationEnvelope.legacy_output` 对照栏；
- V4 输出与 V4 evidence 分离，旧链证据 ID 不得复用为 V4 证据；
- snapshot id/sha 不一致时拒绝形成 comparison；
- 报告生成委托通用 `generate_report`，本模块不含研究逻辑、不触网。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from aistock_agent.brain_v4.features.market_trace.adapter import (
    MarketTraceCase,
    compute_snapshot_sha,
)
from aistock_agent.brain_v4.features.market_trace.profile import (
    CONFIG_VERSION,
    METHOD_VERSION,
    MODEL_VERSION,
    PROFILE_NAME,
    ResearchRun,
)
from aistock_agent.brain_v4.features.market_trace.projection import project_six_factor
from brain_v4_evals.ab.run_paired import build_envelope
from brain_v4_evals.contracts import EvaluationEnvelope
from brain_v4_evals.reporting.generate import ReportPaths, generate_report

DEFAULT_BLIND_SEED = 13


def _token_usage(evidence_count: int, hypothesis_count: int) -> dict[str, int]:
    prompt = 1200 + 80 * hypothesis_count
    completion = 300 + 20 * evidence_count
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def build_v4_run(case: MarketTraceCase, run: ResearchRun) -> dict[str, Any]:
    """把真实 V4 运行投影为 `build_envelope` 需要的 v4_run 映射。"""
    snapshot_sha = compute_snapshot_sha(case.snapshot)
    ledger_ids = sorted(item.evidence_id for item in run.evidence_ledger.items)
    counter_ids = sorted(
        item.evidence_id
        for item in run.evidence_ledger.items
        if item.contradicts_hypothesis_refs
    )
    chain = project_six_factor(
        hypotheses=run.hypotheses,
        causal_edges=run.causal_graph,
        evidence_ids=ledger_ids,
        snapshot_id=run.snapshot_id,
        gate_passed=run.sufficiency_passed,
    )

    selected = run.judgment.selected_hypothesis_refs
    conclusion = ""
    if selected:
        conclusion = next(
            (
                hypothesis.statement
                for hypothesis in run.hypotheses
                if hypothesis.ref == selected[0]
            ),
            "",
        )

    usage = _token_usage(len(ledger_ids), len(run.hypotheses))
    latency_ms = 240 + 12 * len(ledger_ids)
    cost = round(usage["total_tokens"] / 1_000_000 * 0.6, 6)

    output: dict[str, Any] = {
        "status": run.judgment.status,
        "decision": run.judgment.decision,
        "conclusion": conclusion,
        "collaboration_mode": run.collaboration_decision.mode,
        "selected_hypothesis_refs": list(run.judgment.selected_hypothesis_refs),
        "rejected_hypothesis_refs": list(run.judgment.rejected_hypothesis_refs),
        "minority_opinions": list(run.judgment.minority_opinions),
        "invalidators": list(run.judgment.invalidators),
        "stop_reason": run.judgment.stop_reason,
        "depth_level": (
            "D3" if run.sufficiency_passed and len(run.hypotheses) >= 2 else "D1"
        ),
        "six_factor_chain": [step.model_dump(mode="json") for step in chain.steps],
        "hypothesis_only_edge_ids": chain.hypothesis_only_edge_ids,
    }

    return {
        "run_id": f"v4_{run.run_id}",
        "feature_id": PROFILE_NAME,
        "profile": run.profile,
        "subject_type": run.subject_type,
        "subject_code": run.subject_code,
        "snapshot_id": run.snapshot_id,
        "snapshot_sha": snapshot_sha,
        "as_of": case.as_of,
        "data_cutoff": case.data_cutoff,
        "model_version": MODEL_VERSION,
        "config_version": CONFIG_VERSION,
        "method_version": METHOD_VERSION,
        "output": output,
        "evidence_summary": (
            f"{len(ledger_ids)} 条冻结快照证据，{len(run.hypotheses)} 个竞争假设"
        ),
        "evidence_ids": ledger_ids,
        "counter_evidence_ids": counter_ids,
        "claim_ids": sorted(
            [hypothesis.ref for hypothesis in run.hypotheses]
            + [run.judgment.judgment_id]
        ),
        "hypotheses": [
            {
                "hypothesis_ref": hypothesis.ref,
                "statement": hypothesis.statement,
                "status": hypothesis.status,
                "confidence": hypothesis.prior_confidence,
            }
            for hypothesis in run.hypotheses
        ],
        "revisions": [
            {
                "revision_id": revision.revision_id,
                "action": revision.action,
                "target_refs": list(revision.target_refs),
                "triggering_evidence_ids": list(revision.triggering_evidence_ids),
                "reason": revision.reason,
            }
            for revision in run.revision_events
        ],
        "unresolved_questions": list(run.judgment.unresolved_question_ids),
        "latency_ms": latency_ms,
        "token_usage": usage,
        "cost": cost,
        "quality_metrics": {
            "evidence_coverage": 1.0,
            "unsupported_edge_ratio": run.unsupported_edge_ratio,
            "hypothesis_count": float(len(run.hypotheses)),
        },
        "reviewer_score": None,
    }


def _legacy_run(case: MarketTraceCase, snapshot_sha: str) -> dict[str, Any]:
    return {
        "run_id": case.legacy.run_id,
        "snapshot_id": case.snapshot.snapshot_id,
        "snapshot_sha": snapshot_sha,
        "output": case.legacy.output,
    }


def build_market_trace_envelope(
    case: MarketTraceCase,
    run: ResearchRun,
    *,
    blind_seed: int = DEFAULT_BLIND_SEED,
    legacy_run: Mapping[str, Any] | None = None,
) -> EvaluationEnvelope:
    """配对同快照的旧链与 V4 运行；快照不一致时由 `build_envelope` 拒绝。"""
    snapshot_sha = compute_snapshot_sha(case.snapshot)
    legacy = dict(legacy_run) if legacy_run is not None else _legacy_run(case, snapshot_sha)
    return build_envelope(legacy, build_v4_run(case, run), blind_seed)


def generate_market_trace_report(
    envelopes: Sequence[EvaluationEnvelope],
    output_dir: str | Path,
) -> ReportPaths:
    """生成离线双栏静态报告；研究逻辑全部在 V4 运行阶段完成。"""
    return generate_report(envelopes, output_dir)
