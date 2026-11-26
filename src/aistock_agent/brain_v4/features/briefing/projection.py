"""简报投影：把冻结证据窗口压缩为早/午/晚 `BriefingEnvelope`。

约束（计划 Task 9 / 团队接入规范 §6）：
- 早报只输出自身 cutoff 内可见的基线、情景与失效条件；
- 午报只对受影响 claim 追加 `ClaimChange`，每个变化携带 `parent_judgment_id`
  与 `BriefingRelation`；未变化证据按同一 evidence ID 复用；
- 晚报不产生新 claim，只按 ID 引用市场溯源 judgment 并给早/午报 claim 打分；
- 本模块只投影，不调用工具、不读取系统时间。
"""

from __future__ import annotations

from pydantic import Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.briefing import (
    BriefingEnvelope,
    BriefingProfile,
    BriefingRelation,
    EveningBody,
    MiddayBody,
    MorningBody,
)

from .adapter import (
    BriefingChainFixture,
    EvidenceWindow,
    MarketDirection,
    MarketTraceReference,
    RiskLevel,
    ScoreVerdict,
    build_chain_windows,
    evidence_ids_by_source,
)

_RENDER_TARGETS = ["app", "web", "text", "audio"]
_SCHEMA_VERSION = "v1"
_CONFIRMING_RELATIONS: frozenset[str] = frozenset({"confirm", "verify"})
_FALSIFYING_RELATIONS: frozenset[str] = frozenset({"reverse"})


class BriefingClaim(V4Contract):
    """早报声明的一条 claim；后续时段只引用不重写。"""

    claim_ref: str
    source_profile: BriefingProfile
    statement: str
    material: bool = True
    confidence: float = Field(ge=0.0, le=1.0)


class ClaimChange(V4Contract):
    """午报相对父判断的一次显式变化。"""

    claim_ref: str
    parent_judgment_id: str
    relation: BriefingRelation
    reason: str
    evidence_ids: list[str] = Field(default_factory=list)


class ClaimScore(V4Contract):
    """晚报对早/午报 claim 的评分，必须绑定市场溯源 judgment。"""

    claim_ref: str
    source_profile: BriefingProfile
    verdict: ScoreVerdict
    score: float = Field(ge=0.0, le=1.0)
    market_trace_judgment_id: str


class BriefingResult(V4Contract):
    """一个时段的完整投影产物（信封 + 判断链 + 评分）。"""

    envelope: BriefingEnvelope
    judgment_id: str
    direction: MarketDirection
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    claims: list[BriefingClaim] = Field(default_factory=list)
    changes: list[ClaimChange] = Field(default_factory=list)
    scores: list[ClaimScore] = Field(default_factory=list)
    market_trace_judgment_id: str | None = None


class BriefingChainResult(V4Contract):
    """早/午/晚三段结果；parent chain 在构造时已校验。"""

    chain_id: str
    morning: BriefingResult
    midday: BriefingResult
    evening: BriefingResult


def briefing_judgment_id(run_id: str) -> str:
    """时段判断 ID；午/晚报的 parent 引用只允许指向该 ID。"""
    return f"jdg_{run_id}"


def _evidence_ids_for_sources(
    window: EvidenceWindow, source_ids: list[str]
) -> list[str]:
    mapping = evidence_ids_by_source(window)
    missing = [source_id for source_id in source_ids if source_id not in mapping]
    if missing:
        raise ValueError(
            f"证据来源 {missing} 在 {window.profile} 窗口内不可见（cutoff 隔离）"
        )
    return [mapping[source_id] for source_id in source_ids]


def _envelope(
    *,
    run_id: str,
    chain: BriefingChainFixture,
    window: EvidenceWindow,
    profile: BriefingProfile,
    headline: str,
    summary: str,
    body: MorningBody | MiddayBody | EveningBody,
    evidence_ids: list[str],
    risk_flags: list[str],
    parent_run_id: str | None,
    parent_judgment_ids: list[str],
    relation: BriefingRelation | None,
) -> BriefingEnvelope:
    return BriefingEnvelope(
        briefing_id=f"brief_{run_id}",
        trading_date=chain.trading_date,
        profile=profile,
        as_of=window.as_of,
        data_cutoff=window.data_cutoff,
        run_id=run_id,
        parent_run_id=parent_run_id,
        parent_judgment_ids=parent_judgment_ids,
        relation=relation,
        headline=headline,
        summary=summary,
        evidence_ids=sorted(set(evidence_ids)),
        risk_flags=risk_flags,
        body=body,
        render_targets=list(_RENDER_TARGETS),
        schema_version=_SCHEMA_VERSION,
    )


def project_morning(
    chain: BriefingChainFixture, window: EvidenceWindow
) -> BriefingResult:
    """早报：只投影 cutoff 内的基线、情景与失效条件。"""
    if window.profile != "briefing_morning":
        raise ValueError(f"project_morning 需要 briefing_morning 窗口，收到 {window.profile}")
    seed = chain.morning
    evidence_ids = [item.evidence_id for item in window.items]
    key_event_ids = _evidence_ids_for_sources(window, seed.key_event_source_ids)
    body = MorningBody(
        overnight_changes=list(seed.overnight_changes),
        today_baseline=seed.today_baseline,
        key_event_ids=key_event_ids,
        style_sector_hypotheses=list(seed.style_sector_hypotheses),
        scenarios=[scenario.model_copy() for scenario in seed.scenarios],
        invalidators=list(seed.invalidators),
    )
    envelope = _envelope(
        run_id=window.run_id,
        chain=chain,
        window=window,
        profile="briefing_morning",
        headline=seed.headline,
        summary=seed.summary,
        body=body,
        evidence_ids=evidence_ids,
        risk_flags=list(seed.risk_flags),
        parent_run_id=None,
        parent_judgment_ids=[],
        relation=None,
    )
    claims = [
        BriefingClaim(
            claim_ref=hypothesis.claim_ref,
            source_profile="briefing_morning",
            statement=hypothesis.statement,
            material=hypothesis.material,
            confidence=hypothesis.confidence,
        )
        for hypothesis in chain.hypotheses
    ]
    return BriefingResult(
        envelope=envelope,
        judgment_id=briefing_judgment_id(window.run_id),
        direction=seed.direction,
        confidence=seed.confidence,
        risk_level=seed.risk_level,
        claims=claims,
    )


def project_midday(
    chain: BriefingChainFixture,
    window: EvidenceWindow,
    morning: BriefingResult,
) -> BriefingResult:
    """午报：只对受影响 claim 增量修正，未变化证据按 ID 复用。"""
    if window.profile != "briefing_midday":
        raise ValueError(f"project_midday 需要 briefing_midday 窗口，收到 {window.profile}")
    if morning.envelope.profile != "briefing_morning":
        raise ValueError("午报的父判断必须是早报")

    parent_judgment_id = morning.judgment_id
    changes = sorted(
        (
            ClaimChange(
                claim_ref=seed.claim_ref,
                parent_judgment_id=parent_judgment_id,
                relation=seed.relation,
                reason=seed.reason,
                evidence_ids=_evidence_ids_for_sources(
                    window, seed.evidence_source_ids
                ),
            )
            for seed in chain.midday_changes
        ),
        key=lambda change: change.claim_ref,
    )
    confirmed = [
        change.claim_ref
        for change in changes
        if change.relation in _CONFIRMING_RELATIONS
    ]
    falsified = [
        change.claim_ref
        for change in changes
        if change.relation in _FALSIFYING_RELATIONS
    ]

    seed = chain.midday
    reused_ids = set(morning.envelope.evidence_ids)
    window_ids = {item.evidence_id for item in window.items}
    body = MiddayBody(
        confirmed_hypothesis_refs=confirmed,
        falsified_hypothesis_refs=falsified,
        new_drivers=list(seed.new_drivers),
        afternoon_opportunities=list(seed.afternoon_opportunities),
        afternoon_risks=list(seed.afternoon_risks),
    )
    envelope = _envelope(
        run_id=window.run_id,
        chain=chain,
        window=window,
        profile="briefing_midday",
        headline=seed.headline,
        summary=seed.summary,
        body=body,
        evidence_ids=sorted(reused_ids | window_ids),
        risk_flags=list(seed.risk_flags),
        parent_run_id=morning.envelope.run_id,
        parent_judgment_ids=[parent_judgment_id],
        relation="revise",
    )
    return BriefingResult(
        envelope=envelope,
        judgment_id=briefing_judgment_id(window.run_id),
        direction=seed.direction,
        confidence=seed.confidence,
        risk_level=seed.risk_level,
        claims=list(morning.claims),
        changes=changes,
    )


def project_evening(
    chain: BriefingChainFixture,
    window: EvidenceWindow,
    morning: BriefingResult,
    midday: BriefingResult,
    market_trace: MarketTraceReference,
) -> BriefingResult:
    """晚报：按 ID 引用市场溯源 judgment，给早/午报 material claim 打分。"""
    if window.profile != "briefing_evening":
        raise ValueError(f"project_evening 需要 briefing_evening 窗口，收到 {window.profile}")
    if midday.envelope.profile != "briefing_midday":
        raise ValueError("晚报的父判断必须是午报")
    judgment_id = market_trace.judgment_id
    if not judgment_id:
        raise ValueError("晚报必须引用真实的市场溯源 judgment id")

    material_claims = {
        claim.claim_ref: claim for claim in morning.claims if claim.material
    }
    scored_refs = {seed.claim_ref for seed in chain.evening_scores}
    missing = sorted(set(material_claims) - scored_refs)
    if missing:
        raise ValueError(f"晚报未给 material claim 打分: {missing}")
    unknown = sorted(scored_refs - set(material_claims))
    if unknown:
        raise ValueError(f"晚报评分的 claim 不在早报 claims 中: {unknown}")

    scores = [
        ClaimScore(
            claim_ref=seed.claim_ref,
            source_profile=seed.source_profile,
            verdict=seed.verdict,
            score=seed.score,
            market_trace_judgment_id=judgment_id,
        )
        for seed in chain.evening_scores
    ]
    scorecard = [
        f"{score.claim_ref}|{score.verdict}|{score.score:.2f}|{judgment_id}"
        for score in scores
    ]

    seed = chain.evening
    midday_ids = {item.evidence_id for item in window.items}
    body = EveningBody(
        closing_attribution_ref=f"market_trace:{judgment_id}",
        market_trace_judgment_id=judgment_id,
        prediction_scorecard=scorecard,
        errors_and_unresolved=list(seed.errors_and_unresolved),
        next_day_verification=list(seed.next_day_verification),
    )
    envelope = _envelope(
        run_id=window.run_id,
        chain=chain,
        window=window,
        profile="briefing_evening",
        headline=seed.headline,
        summary=seed.summary,
        body=body,
        evidence_ids=sorted(set(midday.envelope.evidence_ids) | midday_ids),
        risk_flags=list(seed.risk_flags),
        parent_run_id=midday.envelope.run_id,
        parent_judgment_ids=[morning.judgment_id, midday.judgment_id],
        relation="verify",
    )
    return BriefingResult(
        envelope=envelope,
        judgment_id=briefing_judgment_id(window.run_id),
        direction=seed.direction,
        confidence=seed.confidence,
        risk_level=seed.risk_level,
        claims=[],
        scores=scores,
        market_trace_judgment_id=judgment_id,
    )


def project_chain(
    chain: BriefingChainFixture, market_trace: MarketTraceReference
) -> BriefingChainResult:
    """构建并校验早/午/晚 parent chain；晚报必须引用市场溯源。"""
    windows = build_chain_windows(chain)
    morning = project_morning(chain, windows["briefing_morning"])
    midday = project_midday(chain, windows["briefing_midday"], morning)
    evening = project_evening(
        chain, windows["briefing_evening"], morning, midday, market_trace
    )

    if midday.envelope.parent_judgment_ids != [morning.judgment_id]:
        raise ValueError("午报 parent chain 未指向早报判断")
    if evening.envelope.parent_judgment_ids != [
        morning.judgment_id,
        midday.judgment_id,
    ]:
        raise ValueError("晚报 parent chain 未同时指向早报与午报判断")
    if not isinstance(evening.envelope.body, EveningBody):
        raise ValueError("晚报正文必须是 EveningBody")
    if evening.envelope.body.market_trace_judgment_id != market_trace.judgment_id:
        raise ValueError("晚报未按 ID 引用市场溯源 judgment")

    return BriefingChainResult(
        chain_id=chain.chain_id,
        morning=morning,
        midday=midday,
        evening=evening,
    )


__all__ = [
    "BriefingChainResult",
    "BriefingClaim",
    "BriefingResult",
    "ClaimChange",
    "ClaimScore",
    "briefing_judgment_id",
    "project_chain",
    "project_evening",
    "project_midday",
    "project_morning",
]
