"""Agent 输入/输出 schema — 供未来 OpenAPI 文档用。

现有 agent 直接使用 ``AgentState``（TypedDict）流转，不强制改造签名。
本模块仅定义文档化模型，为后续 OpenAPI 生成做准备。
"""

from pydantic import BaseModel, Field


class AgentInput(BaseModel):
    """通用 Agent 输入（文档化模型，未强制接入现有 agent）。"""

    messages: list[dict[str, str]] = Field(default_factory=list)
    session_id: str | None = None
    user_id: str | None = None
    favorites: list[str] = Field(default_factory=list)
    intent: str | None = None
    symbol: str | None = None
    tag_code: str | None = None


class AgentOutput(BaseModel):
    """通用 Agent 输出（文档化模型，未强制接入现有 agent）。"""

    final_response: str | None = None
    intent: str | None = None
    symbol: str | None = None
    tag_code: str | None = None
