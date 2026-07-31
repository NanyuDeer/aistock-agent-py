"""个股 Trace 内部 HTTP 契约

定义 Node → Python 个股 Trace 触发请求/响应模型。
"""

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StockTraceTriggerRequest(BaseModel):
    """个股 Trace 触发请求"""

    symbol: str = Field(pattern=r"^\d{6}$")
    cycle: Literal["short", "mid", "long"] | None = None
    report_date: date | None = None
    trace_id: str | None = None

    model_config = ConfigDict(extra="forbid")


class StockTraceTriggerResponse(BaseModel):
    """个股 Trace 触发响应"""

    trace_id: str
    symbol: str
    report_date: date
    status: Literal["completed", "degraded"]
    report_id: str | int | None = None
    degraded_reason: str | None = None
