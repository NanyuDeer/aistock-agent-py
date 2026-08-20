"""Stock Trace 的跨服务契约。

本模块只定义 Python Worker 可读取、校验和交付的结构化对象；不抓取任何
A 股数据，也不包含 LLM 调用。字段名采用 snake_case，对 Node 的 camelCase
响应由 ``services.stock_trace_client`` 统一规范化。
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

SourceLevel = Literal["A", "B", "C", "D"]
SourceKind = Literal[
    "trigger_fact", "quote_fact", "sector_fact", "market_fact",
    "announcement", "news", "capital_fact", "technical_fact",
]
ChainStage = Literal[
    "structural_root", "trigger", "transmission", "exposure", "repricing", "observable_result"
]


class StockSourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    kind: SourceKind
    provider: str
    source_level: SourceLevel
    title: str
    content_excerpt: str
    canonical_url: str | None = None
    source_ref: str | None = None
    symbol: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None
    occurred_at: datetime | None = None
    captured_at: datetime
    freshness_seconds: int | None = Field(default=None, ge=0)
    payload: dict[str, object] = Field(default_factory=dict)
    content_hash: str


class TriggerEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    trigger_revision: int = Field(ge=1)
    symbol: str
    stock_name: str
    trading_date: str
    direction: Literal["up", "down"]
    triggered_at: datetime
    window_start_at: datetime
    window_end_at: datetime
    latest_price: float
    previous_close: float
    actual_value: float
    threshold_value: float
    severity: Literal["medium", "high", "critical"]
    rule_version: str


class StockTraceSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshot_id: str
    event_id: str
    trigger_revision: int = Field(ge=1)
    snapshot_stage: Literal["initial", "enriched", "corrected"]
    source_revision_hash: str
    trigger_event: TriggerEvent
    missing_fields: list[str]
    data_readiness: dict[str, Literal["complete", "partial", "missing"]]
    collector_versions: dict[str, str]
    captured_at: datetime
    supersedes_snapshot_id: str | None = None
    source_records: list[StockSourceRecord]


class TraceCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str
    layer: Literal["company", "sector", "market", "capital", "technical"]
    rank: int = Field(ge=1)
    status: Literal["supported", "weak", "rejected", "insufficient"]
    verdict: str = Field(min_length=1)
    supporting_evidence_ids: list[str]
    counter_evidence_ids: list[str]


class TraceChainNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    node_id: str
    stage: ChainStage
    stage_order: int = Field(ge=1, le=6)
    epistemic_type: Literal["fact", "inference", "hypothesis"]
    status: Literal["established", "partial", "not_established"]
    claim: str = Field(min_length=1)
    evidence_ids: list[str]
    counter_evidence_ids: list[str]


class TraceChain(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chain_id: str
    candidate_id: str
    role: Literal["primary", "alternative"]
    nodes: list[TraceChainNode]


class StockTraceResultPayload(BaseModel):
    """LLM 可填写的归因载荷，不包含系统绑定的身份字段。"""

    model_config = ConfigDict(extra="forbid")

    attribution_status: Literal["confirmed", "hypothesis", "insufficient", "not_applicable"]
    primary_chain_id: str | None = None
    alternative_chain_id: str | None = None
    confidence_score: float = Field(ge=0, le=1)
    confidence_level: Literal["high", "medium", "low"]
    candidates: list[TraceCandidate]
    chains: list[TraceChain]
    contradictions: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    suggested_actions: list[Literal["verify_announcement", "observe", "read_evidence"]]
    # 简短主因短语（关键词/概括性短语，≤20 字），供列表/卡片展示；
    # 无确立主因（insufficient）时给出简短结论（如"证据不足"）。
    primary_phrase: str = Field(min_length=1, max_length=20)


class StockTraceResult(StockTraceResultPayload):
    """可交付结果：身份字段仅由 Worker 从 Job 与冻结快照注入。"""

    schema_version: Literal["stock-trace-result-v1"]
    event_id: str
    snapshot_id: str
    analysis_version: str

    @model_validator(mode="after")
    def _validate_selected_chain_shape(self) -> "StockTraceResult":
        candidate_layers = {candidate.layer for candidate in self.candidates}
        required_layers = {"company", "sector", "market", "capital", "technical"}
        if not required_layers.issubset(candidate_layers):
            raise ValueError(
                "candidates must cover company, sector, market, capital and technical layers"
            )
        selected = {item for item in (self.primary_chain_id, self.alternative_chain_id) if item}
        chain_ids = {chain.chain_id for chain in self.chains}
        if not selected.issubset(chain_ids):
            raise ValueError("selected chain id must exist in chains")
        if self.attribution_status == "confirmed" and not self.primary_chain_id:
            raise ValueError("confirmed result requires a primary chain")
        return self


class StockTraceTriggerRequest(BaseModel):
    """Node → Python 个股 Trace 触发请求。"""

    symbol: str = Field(pattern=r"^\d{6}$")
    cycle: Literal["short", "mid", "long"] | None = None
    report_date: date | None = None
    trace_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class StockTraceTriggerResponse(BaseModel):
    """Python → Node 个股 Trace 触发响应。"""

    trace_id: str
    symbol: str
    report_date: date
    status: Literal["completed", "degraded"]
    report_id: str | int | None = None
    degraded_reason: str | None = None
