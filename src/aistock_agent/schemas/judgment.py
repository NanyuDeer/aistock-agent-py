"""研判契约 —— 智慧大脑唯一结论载体（第一期 P0）。

命名加 Judgment 前缀，避免与 schemas/chat_contract.py 的 Evidence/Insight 冲突。
所有模型 extra="forbid"：多吐字段立即失败，不静默丢弃。
"""

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from aistock_agent.utils.date import add_trading_days

JudgmentKind = Literal["market", "sector", "stock", "event", "fundamental", "sentiment"]
JudgmentDirection = Literal["bullish", "bearish", "neutral"]
JudgmentHorizon = Literal["short", "mid", "long"]
JudgmentQuality = Literal["high", "medium", "low"]

RENDER_TARGET_KEYS: tuple[str, ...] = (
    "home", "stock_page", "event_page", "broadcast", "push", "chat", "pick_page",
)

_JUDGMENT_ID_RE = re.compile(r"^jdg_\d{8}_[A-Za-z0-9_]+$")
_CONFIDENCE_SINGLE_SOURCE_CAP = 0.69
_VERIFY_TRADING_DAYS = 3


class JudgmentEvidence(BaseModel):
    """单条可溯源证据。source 是"独立来源"判定键，不是 channel。"""

    model_config = ConfigDict(extra="forbid")

    evidence_id: str
    kind: JudgmentKind
    subject: str
    claim: str
    source: str
    source_type: str
    published_at: str
    fetched_at: str
    quality: JudgmentQuality = "medium"
    weight: float = 1.0
    value: float = 0.0
    unit: str = ""
    degraded: bool = False


class JudgmentClaim(BaseModel):
    """研究层结论（未定稿），必须挂证据。"""

    model_config = ConfigDict(extra="forbid")

    claim_id: str
    subject: str
    kind: JudgmentKind
    statement: str
    evidence_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    invalidated_by: list[str] = Field(default_factory=list)
    produced_by: str

    @model_validator(mode="after")
    def _need_invalidator(self) -> "JudgmentClaim":
        if not self.invalidated_by:
            raise ValueError("claim must declare at least one invalidator")
        return self


class JudgmentConflict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conflict_id: str
    claim_ids: list[str] = Field(min_length=2)
    description: str
    resolution: Literal["kept", "downgraded", "resolved"] = "kept"


class VerifiedResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["hit", "miss", "insufficient"]
    verified_at: str
    actual: str = ""


class JudgmentRender(BaseModel):
    """7 个出口全量声明：缺键即失败，防止"少写一个出口"的隐式漏渲染。"""

    model_config = ConfigDict(extra="forbid")

    home: bool
    stock_page: bool
    event_page: bool
    broadcast: bool
    push: bool
    chat: bool
    pick_page: bool

    @model_validator(mode="after")
    def _at_least_one(self) -> "JudgmentRender":
        if not any(getattr(self, key) for key in RENDER_TARGET_KEYS):
            raise ValueError("render must enable at least one target")
        return self


class Judgment(BaseModel):
    """研判定稿 —— 所有出口的唯一事实来源。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["judgment.v1"] = "judgment.v1"
    judgment_id: str
    subject: str
    subject_code: str
    subject_type: JudgmentKind
    horizon: JudgmentHorizon
    as_of: str
    due_date: str
    direction: JudgmentDirection
    confidence: float = Field(ge=0.0, le=1.0)
    thesis: str
    evidence: list[JudgmentEvidence] = Field(min_length=1)
    counter_evidence: list[JudgmentEvidence]
    conflicts: list[JudgmentConflict] = Field(default_factory=list)
    invalidators: list[str] = Field(min_length=1)
    verify_at: str
    verified_result: VerifiedResult | None = None
    modules: list[str] = Field(default_factory=list)
    render: JudgmentRender

    @model_validator(mode="after")
    def _check_id_format(self) -> "Judgment":
        if not _JUDGMENT_ID_RE.match(self.judgment_id):
            raise ValueError("judgment_id must match jdg_{YYYYMMDD}_{subject_code}")
        compact = self.as_of.replace("-", "")
        if not self.judgment_id.startswith(f"jdg_{compact}_"):
            raise ValueError("judgment_id date segment must equal as_of")
        return self

    @model_validator(mode="after")
    def _check_verify_window(self) -> "Judgment":
        """硬约束 #4：verify_at 必须精确等于 due_date + 3 个交易日。"""
        expected = add_trading_days(date.fromisoformat(self.due_date), _VERIFY_TRADING_DAYS)
        if self.verify_at != expected.isoformat():
            raise ValueError(f"verify_at must be {expected.isoformat()} (due + 3 trading days)")
        return self

    @model_validator(mode="after")
    def _downgrade_single_source_high_confidence(self) -> "Judgment":
        """硬约束 #2：高置信必须多源；不满足则确定性降级并显式记冲突。"""
        if self.confidence < 0.7:
            return self
        sources = {item.source for item in self.evidence}
        if len(sources) >= 2:
            return self
        self.confidence = _CONFIDENCE_SINGLE_SOURCE_CAP
        self.conflicts = [
            *self.conflicts,
            JudgmentConflict(
                conflict_id=f"cft_{self.judgment_id}_single_source",
                claim_ids=[self.judgment_id, self.judgment_id],
                description="confidence >= 0.7 但独立来源不足 2 个，确定性降级至 0.69",
                resolution="downgraded",
            ),
        ]
        return self
