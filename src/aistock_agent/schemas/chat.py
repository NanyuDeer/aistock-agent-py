"""对话请求/响应 schema — 从 api/routes.py 迁入并补充字段校验。"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """对话请求。"""

    message: str = Field(..., min_length=1, description="用户消息，不可为空")
    session_id: str | None = None
    user_id: str | None = None
    favorites: list[str] = Field(default_factory=list, description="用户自选股代码列表")


class AdvisorSubquestionTrace(BaseModel):
    """单个投顾子问题的来源和降级状态。"""

    intent: str
    reports: list[dict[str, object]]
    sources: list[dict[str, object]]
    as_of: str | None
    missing_sources: list[str]
    degraded: bool


class AdvisorTrace(BaseModel):
    """投顾回答的结构化可追溯状态。"""

    schema_version: str
    subquestions: list[AdvisorSubquestionTrace]
    missing_sources: list[str]
    degraded: bool


class ChatResponse(BaseModel):
    """对话响应。"""

    content: str
    session_id: str
    advisor_trace: AdvisorTrace | None = None
