"""通用评测信封与影子投影契约。

硬约束（团队接入规范 §7、Task 7）：
- 未知字段必须报错（`extra="forbid"`）；
- `is_shadow` 只允许 `True`，影子数据永不进入正式查询；
- 旧链输出与 V4 evidence 物理分离：旧链证据 ID 不得出现在 V4 evidence 中；
- `as_of` / `data_cutoff` 由调用方显式传入，禁止用系统当前时间替代。
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal

from pydantic import Field, model_validator

from aistock_agent.brain_v4.contracts import V4Contract

HypothesisStatus = Literal["open", "supported", "weakened", "rejected", "frozen"]
BlindLabel = Literal["A", "B"]


class EvalSubject(V4Contract):
    """被评测主体：市场、板块或个股。"""

    subject_type: str
    subject_code: str


class EvidenceSummary(V4Contract):
    """V4 证据摘要，只允许引用 V4 自己的证据与 claim。"""

    summary: str
    evidence_ids: list[str] = Field(default_factory=list)
    counter_evidence_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)


class EvalHypothesis(V4Contract):
    """评测快照中的一条假设（不可变记录，不带研究逻辑）。"""

    hypothesis_ref: str
    statement: str
    status: HypothesisStatus
    confidence: float = Field(ge=0.0, le=1.0)


class EvalRevision(V4Contract):
    """评测快照中的一次正式回改。"""

    revision_id: str
    action: str
    target_refs: list[str]
    triggering_evidence_ids: list[str]
    reason: str


class TokenUsage(V4Contract):
    """一次运行的结构化 token 用量。"""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class EvaluationEnvelope(V4Contract):
    """旧链与 V4 在同一冻结快照上的通用评测信封。"""

    feature_id: str
    profile: str
    subject: EvalSubject
    comparison_id: str
    legacy_run_id: str
    v4_run_id: str
    snapshot_id: str
    snapshot_sha: str
    as_of: datetime
    data_cutoff: datetime
    model_version: str
    config_version: str
    method_version: str
    legacy_output: dict[str, Any]
    v4_output: dict[str, Any]
    evidence_summary: EvidenceSummary
    hypotheses: list[EvalHypothesis] = Field(default_factory=list)
    revisions: list[EvalRevision] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    latency_ms: int = Field(ge=0)
    token_usage: TokenUsage
    cost: float = Field(ge=0.0)
    quality_metrics: dict[str, float] = Field(default_factory=dict)
    reviewer_score: float | None = None
    blind_label: BlindLabel
    is_shadow: Literal[True] = True

    @model_validator(mode="after")
    def _reject_legacy_evidence_leak(self) -> "EvaluationEnvelope":
        legacy_ids = _collect_evidence_ids(self.legacy_output)
        v4_ids = set(self.evidence_summary.evidence_ids) | set(
            self.evidence_summary.counter_evidence_ids
        )
        leaked = sorted(legacy_ids & v4_ids)
        if leaked:
            raise ValueError(f"legacy evidence ids cannot be reused as V4 evidence: {leaked}")
        return self

    @model_validator(mode="after")
    def _require_distinct_runs(self) -> "EvaluationEnvelope":
        if self.legacy_run_id == self.v4_run_id:
            raise ValueError("legacy_run_id and v4_run_id must differ")
        return self


def _collect_evidence_ids(output: dict[str, Any]) -> set[str]:
    """从旧链输出中提取它自己的证据 ID（只识别显式的 `evidence_ids` 字段）。"""
    raw = output.get("evidence_ids")
    if not isinstance(raw, list):
        return set()
    return {item for item in raw if isinstance(item, str)}


def select_active(envelopes: Sequence[EvaluationEnvelope]) -> list[EvaluationEnvelope]:
    """正式查询投影：影子信封一律排除。

    当前评测信封只允许 `is_shadow=True`，因此该函数恒返回空列表；它存在的意义
    是给生产查询提供一个稳定的“非影子”入口，避免调用方直接遍历原始信封。
    """
    return [envelope for envelope in envelopes if not envelope.is_shadow]
