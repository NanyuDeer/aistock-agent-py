"""市场溯源 Pydantic 契约 — 事实、证据、候选、因果链和模型输出。

字段名以本文件为准，后续任务不得另起近义字段。
本模块只定义数据结构，不包含任何业务逻辑或 LLM 调用。
"""

from datetime import datetime
from typing import Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 市场现象 kind 的 Literal 别名。
MarketPhenomenonKind: TypeAlias = Literal[
    "broad_rally",
    "broad_decline",
    "style_divergence",
    "sector_concentration",
    "sentiment_extreme",
]


class SourceRecord(BaseModel):
    source_id: str
    kind: Literal["market_fact", "event_evidence"]
    provider: str
    title: str
    content: str
    url: str | None
    occurred_at: datetime | None
    captured_at: datetime
    source_level: Literal["primary", "reporting", "market_data"]


class DataReadiness(BaseModel):
    market_data: Literal["complete", "incomplete"]
    attribution_inputs: Literal["complete", "partial", "missing"]
    causal_evidence: Literal["ready", "partial", "not_ready"]


class DataAvailability(BaseModel):
    """Availability of a market fact in the frozen snapshot."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["available", "partial", "unavailable"]
    available_fields: list[str] = Field(default_factory=list)
    approximate: bool = False
    reason: str | None = None


class SourceCollectionStatus(BaseModel):
    """Outcome of collecting one auxiliary source family."""

    model_config = ConfigDict(extra="forbid")

    state: Literal["available", "empty", "unavailable", "invalid_for_causality"]
    provider: str
    item_count: int = 0
    reason: str | None = None


class RuleDiagnostic(BaseModel):
    rule: str
    matched: bool
    evidence_ids: list[str]


class DetectedPhenomenon(BaseModel):
    kind: MarketPhenomenonKind
    summary: str
    fact_ids: list[str]
    tags: list[str]
    severity: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def _require_fact_ids(self) -> "DetectedPhenomenon":
        if not self.fact_ids:
            raise ValueError("detected phenomenon fact_ids must not be empty")
        return self


class PhenomenonDiscoveryResult(BaseModel):
    status: Literal["detected", "no_phenomenon", "insufficient_data"]
    primary: DetectedPhenomenon | None
    concurrent_phenomena: list[DetectedPhenomenon]
    data_readiness: DataReadiness
    diagnostics: list[RuleDiagnostic]

    @model_validator(mode="after")
    def _validate_status_shape(self) -> "PhenomenonDiscoveryResult":
        if self.status == "detected":
            if self.primary is None:
                raise ValueError("detected discovery requires primary")
        elif self.primary is not None or self.concurrent_phenomena:
            raise ValueError("non-detected discovery must not contain phenomena")
        return self


class CausalNode(BaseModel):
    stage: Literal[
        "structural_root",
        "trigger",
        "transmission",
        "exposure",
        "repricing",
        "observable_result",
    ]
    claim: str
    evidence_ids: list[str]


class CausalChain(BaseModel):
    nodes: list[CausalNode]


class CandidateExplanation(BaseModel):
    id: str
    category: Literal[
        "global_risk_liquidity",
        "domestic_macro_policy",
        "industry_technology_supply",
        "market_positioning_liquidity",
    ]
    status: Literal["supported", "weak", "rejected", "insufficient"]
    verdict: str
    chain: CausalChain | None
    supporting_evidence_ids: list[str]
    counter_evidence_ids: list[str]


class MarketTraceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.1"]
    attribution_status: Literal["confirmed", "hypothesis", "insufficient", "not_applicable"]
    candidates: list[CandidateExplanation]
    primary_chain_id: str | None
    alternative_chain_id: str | None
    confidence: Literal["high", "medium", "low"]
    unresolved_questions: list[str]


class MarketTraceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    trade_date: str
    captured_at: datetime
    a_share: dict[str, object]
    sources: dict[str, SourceRecord]
    missing_fields: list[str]
    phenomenon_discovery: PhenomenonDiscoveryResult
    data_availability: dict[str, DataAvailability] = Field(default_factory=dict)
    collection_status: dict[str, SourceCollectionStatus] = Field(default_factory=dict)


class ReviewArtifact(BaseModel):
    schema_version: Literal["1.1"]
    snapshot: MarketTraceSnapshot
    trace: MarketTraceResult
    markdown: str
    trace_summary: str
    sectors: list[str]
