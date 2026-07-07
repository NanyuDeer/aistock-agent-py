"""SSE 事件 schema — ``SSEEvent`` Pydantic 模型。

``type`` 字段校验取值必须在 ``constants.SSEEventType`` 集合内。
本模型供未来 OpenAPI 文档与 SSE 事件校验使用，当前 stream 仍直接 yield dict。
"""

from pydantic import BaseModel, field_validator

from aistock_agent.constants import SSEEventType

_VALID_SSE_TYPES = frozenset({
    SSEEventType.TOOL_START,
    SSEEventType.TOOL_END,
    SSEEventType.LLM_START,
    SSEEventType.TEXT,
    SSEEventType.DONE,
    SSEEventType.ERROR,
})


class SSEEvent(BaseModel):
    """SSE 事件模型，``type`` 取值必须在 SSEEventType 集合内。"""

    type: str
    # 其余字段按事件类型可选
    tool: str | None = None
    label: str | None = None
    content: str | None = None
    message: str | None = None
    args: dict[str, str] | None = None

    @field_validator("type")
    @classmethod
    def _validate_type(cls, v: str) -> str:
        if v not in _VALID_SSE_TYPES:
            raise ValueError(f"Invalid SSE event type: {v}")
        return v
