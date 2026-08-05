"""自选股洞察归因的跨服务契约。

定义 LLM ``with_structured_output`` 的目标 schema（``DriverOutput`` /
``InsightAttributionOutput``）以及回写 Node 的结果载荷（``InsightResultPayload``）。
类别枚举严格五类，与 ``services.insight_candidate.CATEGORIES`` 一致。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Category = Literal[
    "industry_theme", "company_event", "earnings", "market", "trading_sentiment"
]
Confidence = Literal["high", "medium", "low"]


class DriverOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(description="从候选集选择的候选 ID（如 c1/c2），必须存在于候选集")
    label: str = Field(description="主题概括关键词，可沿用候选 label 或基于证据概括，1-12 字")
    confidence: Confidence = Field(description="该主因的置信度")


class InsightAttributionOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    attribution_status: Literal["confirmed", "unconfirmed"]
    primary_driver: DriverOutput | None = Field(description="主因；unconfirmed 时为 None")
    secondary_drivers: list[DriverOutput] = Field(
        default_factory=list, max_length=2, description="次因，最多 2 个"
    )


class InsightResultPayload(BaseModel):
    """回写 Node 的结果（身份字段由 worker 注入）"""

    model_config = ConfigDict(extra="forbid")

    attribution_status: Literal["confirmed", "unconfirmed"]
    confidence: Confidence | Literal["unconfirmed"]
    primary_driver: dict[str, object]  # {label, category, confidence, evidence_quote, source_ids}
    secondary_drivers: list[dict[str, object]]
    display_report: dict[str, object]  # 规范14 双层
    podcast_brief: str
    validation_status: Literal["llm", "rule_fallback"]
    model_provider: str
