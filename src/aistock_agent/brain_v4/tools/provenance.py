"""统一工具网关的 provenance 信封与错误分类。

约束（规范 §5.4）：每条证据必须携带来源、`event_time`、`published_at`、
`fetched_at`、`as_of`、质量与错误分类。本模块只定义信封结构，不读取系统
时间：`as_of` / `fetched_at` 均必须由调用方显式注入。
"""

from datetime import datetime
from typing import Any, Literal

from ..contracts import V4Contract

QualityLevel = Literal["high", "medium", "low"]

#: 网关错误分类。`error` 为 None 表示成功；其余取值对应明确失败路径。
GatewayError = Literal[
    "not_allowed",
    "timeout",
    "future_data",
    "upstream_error",
    "invalid_params",
]


class ToolEnvelope(V4Contract):
    """一次工具调用的可追溯信封。

    `data` 在失败时恒为 None；`error` 必须能区分未来数据、越权工具、
    超时和上游异常，便于下游按类降级而不是一律重试。
    """

    tool: str
    params: dict[str, Any]
    evidence_id: str
    event_time: datetime | None = None
    published_at: datetime | None = None
    fetched_at: datetime
    as_of: datetime
    quality: QualityLevel = "medium"
    error: GatewayError | None = None
    error_detail: str | None = None
    data: Any | None = None
