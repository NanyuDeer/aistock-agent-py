"""研判层契约：V4 唯一对外判断记录。

结论完整性用 `status`（ok|partial|insufficient）表达，停止原因用 `stop_reason`
独立记录，两者不得互相替代（团队接入规范 §5.6）。
"""

from datetime import datetime
from typing import Literal

from pydantic import Field

from . import V4Contract


class V4Judgment(V4Contract):
    """单写者产出的研判记录，供 ledger 落账与各出口投影。"""

    judgment_id: str
    run_id: str
    profile: str
    subject_type: str
    subject_code: str
    horizon: str
    as_of: datetime
    data_cutoff: datetime
    snapshot_id: str
    decision: Literal["accept", "revise", "insufficient", "escalate"]
    status: Literal["ok", "partial", "insufficient"]
    stop_reason: str
    selected_hypothesis_refs: list[str] = Field(default_factory=list)
    rejected_hypothesis_refs: list[str] = Field(default_factory=list)
    minority_opinions: list[str] = Field(default_factory=list)
    invalidators: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    unresolved_question_ids: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    #: 分析师基于冻结证据给出的最终叙事（只拼接已有 claim，不新增事实）。
    #: 默认空串，保证旧调用方/旧归档反序列化零破坏。
    narrative: str = ""
    #: 叙事中引用、且确实存在于冻结 ledger 的证据 ID（账本外引用一律剔除）。
    narrative_evidence_ids: list[str] = Field(default_factory=list)
    parent_judgment_ids: list[str] = Field(default_factory=list)
    relation: str | None = None
    config_version: str
    model_version: str
    prompt_version: str
    method_version: str
    created_at: datetime
