"""V4 工具层：统一证据网关、provenance 信封与 memo 去重。

规范 §3：`tools/` 为黄区，负责统一工具网关、snapshot、provenance 与 memo。
"""

from .gateway import DEFAULT_ALLOWED_TOOLS, ToolClient, ToolClientResult, ToolGateway
from .memo import Memo, evidence_id_for, memo_key, normalize_params
from .provenance import GatewayError, QualityLevel, ToolEnvelope

__all__ = [
    "DEFAULT_ALLOWED_TOOLS",
    "GatewayError",
    "Memo",
    "QualityLevel",
    "ToolClient",
    "ToolClientResult",
    "ToolEnvelope",
    "ToolGateway",
    "evidence_id_for",
    "memo_key",
    "normalize_params",
]
