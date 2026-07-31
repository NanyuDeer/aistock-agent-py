"""市场复盘问答 - 请求/响应 Pydantic 契约。

本模块只定义数据结构，不包含业务逻辑或 LLM 调用。
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, StrictStr, field_validator


def parse_market_trace_report_date(value: str) -> date:
    """解析严格的 ``YYYY-MM-DD`` 报告日期。"""
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("report_date 必须是合法 YYYY-MM-DD 日期") from exc
    if parsed.isoformat() != value:
        raise ValueError("report_date 必须是合法 YYYY-MM-DD 日期")
    return parsed


class MarketTraceQaRequest(BaseModel):
    """市场复盘问答请求。"""

    message: str
    report_date: StrictStr | None = None
    session_id: str | None = None

    @field_validator("report_date")
    @classmethod
    def _validate_report_date(cls, value: str | None) -> str | None:
        if value is not None:
            parse_market_trace_report_date(value)
        return value


class MarketTraceQaSource(BaseModel):
    """证据来源摘要（从冻结 sources 中提取，不含原始全文）。"""

    source_id: str
    title: str
    kind: Literal["market_fact", "event_evidence"]
    provider: str


class MarketTraceQaTrace(BaseModel):
    """每条回答的溯源元数据。"""

    artifact_id: str
    sources: list[MarketTraceQaSource]
    as_of: str
    confidence: Literal["high", "medium", "low"]
    uncertainty: list[str]
    degraded: bool
    degraded_reason: str | None = None


class MarketTraceQaResponse(BaseModel):
    """市场复盘问答响应。"""

    content: str
    session_id: str
    trace: MarketTraceQaTrace
