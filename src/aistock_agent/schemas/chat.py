"""对话请求/响应 schema — 从 api/routes.py 迁入并补充字段校验。"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """对话请求。"""

    message: str = Field(..., min_length=1, description="用户消息，不可为空")
    session_id: str | None = None
    user_id: str | None = None
    favorites: list[str] = Field(default_factory=list, description="用户自选股代码列表")


class ChatResponse(BaseModel):
    """对话响应。"""

    content: str
    session_id: str
