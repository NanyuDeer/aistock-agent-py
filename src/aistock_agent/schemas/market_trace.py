"""市场溯源 Pydantic 契约 — 事实、证据、候选、因果链和模型输出。

字段名以本文件为准，后续任务不得另起近义字段。
本模块只定义数据结构，不包含任何业务逻辑或 LLM 调用。
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel


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


class DominantPhenomenon(BaseModel):
    kind: Literal[
        "broad_rally",
        "broad_decline",
        "style_divergence",
        "sector_concentration",
        "sentiment_extreme",
    ]
    summary: str
    fact_ids: list[str]
    score: int


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
    schema_version: Literal["1.0"]
    dominant_phenomenon: DominantPhenomenon | None
    candidates: list[CandidateExplanation]
    primary_chain_id: str | None
    alternative_chain_id: str | None
    confidence: Literal["high", "medium", "low"]
    unresolved_questions: list[str]


class MarketTraceSnapshot(BaseModel):
    snapshot_id: str
    trade_date: str
    captured_at: datetime
    a_share: dict[str, object]
    sources: dict[str, SourceRecord]
    missing_fields: list[str]
    dominant_phenomenon: DominantPhenomenon | None


class ReviewArtifact(BaseModel):
    schema_version: Literal["1.0"]
    snapshot: MarketTraceSnapshot
    trace: MarketTraceResult
    markdown: str
    trace_summary: str
    sectors: list[str]
