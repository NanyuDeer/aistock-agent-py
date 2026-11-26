"""早/午/晚简报评测：为每个时段生成 `EvaluationEnvelope` 与双栏静态报告。

边界（计划 Task 9 Step 5 / 团队接入规范 §7）：
- 旧链输出只进入 `legacy_output` 对照栏，旧链证据 ID 不得复用为 V4 证据；
- 旧链与 V4 共享同一时段 fetch 快照摘要，snapshot 不一致时由 `build_envelope` 拒绝；
- 晚报的 `EvaluationEnvelope` 必须携带真实市场溯源 judgment id；
- 报告生成委托通用 `generate_report`，本模块不含研究逻辑、不触网。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any, Protocol

from aistock_agent.brain_v4.contracts.briefing import EveningBody
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.judgment import V4Judgment
from brain_v4_evals.ab.run_paired import build_envelope
from brain_v4_evals.contracts import EvaluationEnvelope
from brain_v4_evals.reporting.generate import ReportPaths, generate_report

from .adapter import (
    BriefingChainFixture,
    BriefingSessionSeed,
    MarketTraceReference,
    build_evidence_window,
    compute_window_sha,
)
from .projection import BriefingResult

DEFAULT_BLIND_SEED = 13


class MarketTraceRunLike(Protocol):
    """Task 8 `run_market_trace` 产物中晚报所需的最小只读视图。"""

    run_id: str
    snapshot_id: str
    judgment: V4Judgment
    evidence_ledger: EvidenceLedger


def _session_for(
    chain: BriefingChainFixture, result: BriefingResult
) -> BriefingSessionSeed:
    for session in chain.sessions:
        if session.profile == result.envelope.profile:
            return session
    raise ValueError(f"fixture 缺少时段 {result.envelope.profile!r} 的运行边界")


def _token_usage(evidence_count: int, claim_count: int) -> dict[str, int]:
    prompt = 900 + 60 * claim_count
    completion = 200 + 15 * evidence_count
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def market_trace_reference(run: MarketTraceRunLike) -> MarketTraceReference:
    """把 Task 8 研判投影为晚报可引用的稳定 ID 引用，不复制归因。"""
    return MarketTraceReference(
        run_id=run.run_id,
        judgment_id=run.judgment.judgment_id,
        snapshot_id=run.snapshot_id,
        decision=run.judgment.decision,
        status=run.judgment.status,
        evidence_ids=sorted(item.evidence_id for item in run.evidence_ledger.items),
        selected_claim_refs=list(run.judgment.selected_hypothesis_refs),
    )


def _v4_output(result: BriefingResult) -> dict[str, Any]:
    envelope = result.envelope
    return {
        "headline": envelope.headline,
        "summary": envelope.summary,
        "direction": result.direction,
        "confidence": result.confidence,
        "risk_level": result.risk_level,
        "risk_flags": list(envelope.risk_flags),
        "relation": envelope.relation,
        "parent_run_id": envelope.parent_run_id,
        "parent_judgment_ids": list(envelope.parent_judgment_ids),
        "judgment_id": result.judgment_id,
        "claim_changes": [change.model_dump(mode="json") for change in result.changes],
        "scorecard": [score.model_dump(mode="json") for score in result.scores],
        "market_trace_judgment_id": result.market_trace_judgment_id,
        "body": envelope.body.model_dump(mode="json"),
    }


def _legacy_run(session: BriefingSessionSeed, snapshot_sha: str) -> dict[str, Any]:
    return {
        "run_id": session.legacy_run_id,
        "snapshot_id": session.snapshot_id,
        "snapshot_sha": snapshot_sha,
        "output": {
            "headline": session.legacy_headline,
            "evidence_ids": list(session.legacy_evidence_ids),
        },
    }


def _v4_run(
    chain: BriefingChainFixture,
    session: BriefingSessionSeed,
    result: BriefingResult,
) -> dict[str, Any]:
    window = build_evidence_window(chain, session.profile)
    evidence_ids = sorted(item.evidence_id for item in window.items)
    claims = list(result.claims)
    usage = _token_usage(len(evidence_ids), len(claims))
    return {
        "run_id": f"v4_{result.envelope.run_id}",
        "feature_id": "briefing",
        "profile": session.profile,
        "subject_type": "market",
        "subject_code": chain.subject_code,
        "snapshot_id": session.snapshot_id,
        "snapshot_sha": compute_window_sha(window),
        "as_of": session.as_of,
        "data_cutoff": session.data_cutoff,
        "model_version": "v4-briefing-1",
        "config_version": "cfg_briefing_v1",
        "method_version": "method_v4.1",
        "output": _v4_output(result),
        "evidence_summary": (
            f"{len(evidence_ids)} 条冻结时段证据，{len(claims)} 条 claim"
        ),
        "evidence_ids": evidence_ids,
        "counter_evidence_ids": [],
        "claim_ids": sorted(
            {claim.claim_ref for claim in claims}
            | {change.claim_ref for change in result.changes}
            | {result.judgment_id}
        ),
        "hypotheses": [
            {
                "hypothesis_ref": claim.claim_ref,
                "statement": claim.statement,
                "status": "open",
                "confidence": claim.confidence,
            }
            for claim in claims
        ],
        "revisions": [
            {
                "revision_id": f"rev_{result.envelope.run_id}_{change.claim_ref}",
                "action": change.relation,
                "target_refs": [change.claim_ref],
                "triggering_evidence_ids": list(change.evidence_ids),
                "reason": change.reason,
            }
            for change in result.changes
        ],
        "unresolved_questions": (
            list(result.envelope.body.errors_and_unresolved)
            if isinstance(result.envelope.body, EveningBody)
            else []
        ),
        "latency_ms": 240 + 12 * len(evidence_ids),
        "token_usage": usage,
        "cost": round(usage["total_tokens"] / 1_000_000 * 0.6, 6),
        "quality_metrics": {
            "evidence_coverage": 1.0,
            "parent_chain_depth": float(len(result.envelope.parent_judgment_ids)),
            "claim_count": float(len(claims)),
        },
        "reviewer_score": None,
    }


def build_briefing_envelope(
    chain: BriefingChainFixture, result: BriefingResult
) -> EvaluationEnvelope:
    """把单时段简报结果配对为旧链/V4 评测信封。"""
    session = _session_for(chain, result)
    window = build_evidence_window(chain, session.profile)
    snapshot_sha = compute_window_sha(window)
    return build_envelope(
        _legacy_run(session, snapshot_sha),
        _v4_run(chain, session, result),
        DEFAULT_BLIND_SEED,
    )


def generate_briefing_reports(
    chain: BriefingChainFixture,
    results: Sequence[BriefingResult],
    output_dir: str | Path,
) -> ReportPaths:
    """生成三时段双栏静态报告；研究逻辑全部在投影阶段完成。"""
    envelopes = [build_briefing_envelope(chain, result) for result in results]
    return generate_report(envelopes, output_dir)


__all__ = [
    "DEFAULT_BLIND_SEED",
    "MarketTraceRunLike",
    "build_briefing_envelope",
    "generate_briefing_reports",
    "market_trace_reference",
]
