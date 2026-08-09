"""预测能力 Pydantic 契约 — 影响持续性推演输出。

字段名以本文件为准，后续任务不得另起近义字段。
本模块只定义数据结构，不包含任何业务逻辑或 LLM 调用。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# 时间尺度分档（对齐 alert cycle）：短 1-5 交易日 / 中 1-4 周 / 长 1-6 月
PredictionHorizonType = Literal["short", "mid", "long"]
# 当前演化阶段
PredictionPhase = Literal["building", "peaking", "decaying", "returning"]


class PredictionHorizon(BaseModel):
    """单档位影响持续性预测。"""

    model_config = ConfigDict(extra="forbid")

    horizon: PredictionHorizonType
    remaining_estimate: str  # 还能持续多久（定性估算，如 "2-4 周"）
    phase: PredictionPhase  # 当前演化阶段
    direction: Literal["bullish", "bearish", "neutral"]  # 该档位影响方向
    target: str  # 验证对象（"上证指数"/"半导体板块"）
    metric_projection: str  # 可量化预期（到期 hit/miss 对照依据）
    confidence: Literal["high", "medium", "low"]


class PredictionRisk(BaseModel):
    """风险因素 — 什么会导致预测失效。"""

    model_config = ConfigDict(extra="forbid")

    factor: str
    invalidation: str


class PredictionResult(BaseModel):
    """影响持续性推演完整输出。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"]
    prediction_status: Literal["confirmed", "hypothesis", "insufficient"]
    horizons: list[PredictionHorizon] = Field(...)  # 多档位并存
    evolution_narrative: str  # 后续演化路径叙事（强化→衰减→回归）
    risks: list[PredictionRisk]
    evidence_ids: list[str]  # 只引用溯源证据，禁止编造外部事实
    attribution_summary: str | None = None  # 一句话预测结论（随报告展示）

    @model_validator(mode="after")
    def _require_horizons(self) -> "PredictionResult":
        if not self.horizons:
            raise ValueError("horizons must not be empty")
        return self
