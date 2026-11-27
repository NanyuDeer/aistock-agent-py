"""早午晚报冻结 fixture 适配器：按每个时段的 cutoff 过滤证据窗口。

边界（计划 Task 9 Step 1 / 团队接入规范 §7）：
- `as_of` / `data_cutoff` / `run_id` / `snapshot_id` 全部来自冻结 fixture，
  本模块不读取系统时间；
- 一个事件只有在 `event_time <= data_cutoff` 且 `published_at <= as_of` 时才可见，
  防止 cutoff 之后发布的消息或未来事件泄漏进早报；
- 三个时段各自分配独立 run / snapshot / cutoff / ledger namespace，只共享冻结来源；
- 不导入旧 morning worker、旧链 state 或旧链最终结论。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.briefing import (
    BriefingProfile,
    BriefingRelation,
    Scenario,
)
from aistock_agent.brain_v4.contracts.evidence import (
    EvidenceItem,
    EvidenceKind,
    QualityLevel,
)

from .profiles import briefing_ledger_namespace

MarketDirection = Literal["up", "down", "range"]
RiskLevel = Literal["low", "medium", "high"]
ScoreVerdict = Literal["confirmed", "reversed", "unresolved"]

_SESSION_ORDER: tuple[BriefingProfile, ...] = (
    "briefing_morning",
    "briefing_midday",
    "briefing_evening",
)


class BriefingEvidenceSeed(V4Contract):
    """一条冻结来源；只有时间边界通过后才进入某时段的证据窗口。"""

    source_id: str
    source: str
    source_type: str
    title: str
    content: str
    event_time: datetime
    published_at: datetime
    data_quality: QualityLevel = "high"
    time_confidence: QualityLevel = "high"
    evidence_kind: EvidenceKind = "direct"
    tags: list[str] = Field(default_factory=list)


class BriefingHypothesisSeed(V4Contract):
    """早报的候选假设；只声明 V4 自己的 claim，不携带旧链结论。"""

    claim_ref: str
    statement: str
    material: bool = True
    confidence: float = Field(ge=0.0, le=1.0)


class BriefingSessionSeed(V4Contract):
    """一个时段的冻结运行边界与旧链对照元数据。"""

    profile: BriefingProfile
    run_id: str
    snapshot_id: str
    as_of: datetime
    data_cutoff: datetime
    legacy_run_id: str
    legacy_headline: str
    legacy_evidence_ids: list[str] = Field(default_factory=list)


class MiddayChangeSeed(V4Contract):
    """午报相对早报的一次显式变化；证据按冻结来源 ID 解析。"""

    claim_ref: str
    relation: BriefingRelation
    reason: str
    evidence_source_ids: list[str] = Field(min_length=1)


class EveningScoreSeed(V4Contract):
    """晚报对早/午报 claim 的评分项；必须绑定市场溯源 judgment。"""

    claim_ref: str
    source_profile: BriefingProfile
    verdict: ScoreVerdict
    score: float = Field(ge=0.0, le=1.0)


class MorningSeed(V4Contract):
    headline: str
    summary: str
    direction: MarketDirection
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    overnight_changes: list[str] = Field(default_factory=list)
    today_baseline: str
    key_event_source_ids: list[str] = Field(default_factory=list)
    style_sector_hypotheses: list[str] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(min_length=1)
    invalidators: list[str] = Field(min_length=1)
    risk_flags: list[str] = Field(default_factory=list)


class MiddaySeed(V4Contract):
    headline: str
    summary: str
    direction: MarketDirection
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    new_drivers: list[str] = Field(default_factory=list)
    afternoon_opportunities: list[str] = Field(default_factory=list)
    afternoon_risks: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class EveningSeed(V4Contract):
    headline: str
    summary: str
    direction: MarketDirection
    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: RiskLevel
    errors_and_unresolved: list[str] = Field(default_factory=list)
    next_day_verification: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)


class MarketTraceLink(V4Contract):
    """晚报要引用的 Task 8 冻结案例；judgment id 由真实运行解出，不写死。"""

    case_id: str
    case_fixture: str
    judgment_role: str


class BriefingChainFixture(V4Contract):
    """一个交易日的早/午/晚冻结链。"""

    chain_id: str
    trading_date: date
    subject_code: str
    sessions: list[BriefingSessionSeed]
    items: list[BriefingEvidenceSeed]
    hypotheses: list[BriefingHypothesisSeed]
    midday_changes: list[MiddayChangeSeed]
    evening_scores: list[EveningScoreSeed]
    morning: MorningSeed
    midday: MiddaySeed
    evening: EveningSeed
    market_trace: MarketTraceLink

    @model_validator(mode="after")
    def _require_three_distinct_sessions(self) -> BriefingChainFixture:
        profiles = [session.profile for session in self.sessions]
        if tuple(profiles) != _SESSION_ORDER:
            raise ValueError(
                "briefing fixture 必须按早/午/晚顺序声明三个时段，实际为 "
                f"{profiles!r}"
            )
        if len({session.run_id for session in self.sessions}) != len(profiles):
            raise ValueError("每个时段必须使用独立 run_id")
        if len({session.snapshot_id for session in self.sessions}) != len(profiles):
            raise ValueError("每个时段必须使用独立 snapshot_id")
        return self


class EvidenceWindow(V4Contract):
    """某时段在自身 cutoff 下可见的冻结证据集合。"""

    profile: BriefingProfile
    run_id: str
    snapshot_id: str
    as_of: datetime
    data_cutoff: datetime
    ledger_namespace: str
    items: list[EvidenceItem] = Field(default_factory=list)


class MarketTraceReference(V4Contract):
    """晚报可引用的市场溯源研判投影；只按 ID 引用，不复制归因逻辑。"""

    run_id: str
    judgment_id: str
    snapshot_id: str
    decision: str
    status: str
    evidence_ids: list[str] = Field(default_factory=list)
    selected_claim_refs: list[str] = Field(default_factory=list)


def _canonical(payload: object) -> str:
    return json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def load_briefing_chain(fixture_path: str | Path) -> BriefingChainFixture:
    """读取冻结日内链；校验失败直接报错，不做静默补全。"""
    raw = json.loads(Path(fixture_path).read_text(encoding="utf-8"))
    return BriefingChainFixture.model_validate(raw)


def _session_for(
    chain: BriefingChainFixture, profile: BriefingProfile
) -> BriefingSessionSeed:
    for session in chain.sessions:
        if session.profile == profile:
            return session
    raise KeyError(f"briefing fixture 缺少时段 {profile!r}")


def is_visible(seed: BriefingEvidenceSeed, session: BriefingSessionSeed) -> bool:
    """时间纪律：事件须已发生且已发布，才允许进入该时段证据窗口。"""
    return seed.event_time <= session.data_cutoff and seed.published_at <= session.as_of


def evidence_id_for(
    seed: BriefingEvidenceSeed, session: BriefingSessionSeed
) -> str:
    """种子 → 证据 ID 的唯一公式。

    公开出来是为了让运行层能按 `evidence_id` 精确回填证据正文；此前运行层按
    提供方聚合内容，导致 7 条同源证据被塞进同一段混合文本（000300 的行里出现
    000001 的数字），模型据此误判"数据缺失"。
    """
    return f"ev_brief_{_digest({'snapshot_id': session.snapshot_id, 'source_id': seed.source_id})[:12]}_{seed.source_id.lower()}"


def _to_evidence(
    seed: BriefingEvidenceSeed, session: BriefingSessionSeed
) -> EvidenceItem:
    evidence_id = evidence_id_for(seed, session)
    return EvidenceItem(
        evidence_id=evidence_id,
        source=seed.source,
        source_type=seed.source_type,
        event_time=seed.event_time,
        published_at=seed.published_at,
        fetched_at=session.as_of,
        as_of=session.as_of,
        evidence_kind=seed.evidence_kind,
        supports_hypothesis_refs=[],
        contradicts_hypothesis_refs=[],
        data_quality=seed.data_quality,
        time_confidence=seed.time_confidence,
        tool="frozen_briefing_fixture",
        tool_params={
            "source_id": seed.source_id,
            "title": seed.title,
            "tags": ",".join(seed.tags),
        },
        response_digest=_digest(seed.model_dump(mode="json")),
        snapshot_id=session.snapshot_id,
    )


def build_evidence_window(
    chain: BriefingChainFixture, profile: BriefingProfile
) -> EvidenceWindow:
    """按 profile 的 cutoff/as_of 过滤证据，返回该时段独立窗口。"""
    session = _session_for(chain, profile)
    items = [
        _to_evidence(seed, session)
        for seed in chain.items
        if is_visible(seed, session)
    ]
    return EvidenceWindow(
        profile=session.profile,
        run_id=session.run_id,
        snapshot_id=session.snapshot_id,
        as_of=session.as_of,
        data_cutoff=session.data_cutoff,
        ledger_namespace=briefing_ledger_namespace(session.profile, session.run_id),
        items=sorted(items, key=lambda item: item.evidence_id),
    )


def build_chain_windows(
    chain: BriefingChainFixture,
) -> dict[str, EvidenceWindow]:
    """一次构建早/午/晚三个独立证据窗口。"""
    return {
        session.profile: build_evidence_window(chain, session.profile)
        for session in chain.sessions
    }


def compute_window_sha(window: EvidenceWindow) -> str:
    """对某时段冻结证据集合求 SHA-256；同一窗口必然同一摘要。"""
    payload = [item.model_dump(mode="json") for item in window.items]
    return _digest(payload)


def evidence_ids_by_source(window: EvidenceWindow) -> dict[str, str]:
    """来源 ID -> evidence ID；用于把 fixture 里的变化证据解析为真实证据引用。"""
    return {
        str(item.tool_params["source_id"]): item.evidence_id
        for item in window.items
    }


__all__ = [
    "BriefingChainFixture",
    "BriefingEvidenceSeed",
    "BriefingHypothesisSeed",
    "BriefingSessionSeed",
    "EveningScoreSeed",
    "EveningSeed",
    "EvidenceWindow",
    "MarketDirection",
    "MarketTraceLink",
    "MarketTraceReference",
    "MiddayChangeSeed",
    "MiddaySeed",
    "MorningSeed",
    "RiskLevel",
    "ScoreVerdict",
    "build_chain_windows",
    "build_evidence_window",
    "compute_window_sha",
    "evidence_ids_by_source",
    "is_visible",
    "load_briefing_chain",
]
