"""对话请求/响应 schema — 从 api/routes.py 迁入并补充字段校验。"""

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    """对话请求。"""

    message: str = Field(..., min_length=1, description="用户消息，不可为空")
    session_id: str | None = None
    user_id: str | None = None
    favorites: list[str] = Field(default_factory=list, description="用户自选股代码列表")
    force_deep: bool = Field(default=False, description="强制深度分析（对齐 ws.py，D4）")


class ChatResponse(BaseModel):
    """对话响应。"""

    content: str
    session_id: str
    # P10 线 2 缺口修复：HTTP 非流式降级路径透出本轮 token 用量（graph 未采集时为 None，null 兼容）
    token_usage: dict[str, int] | None = None
    # 深度分析引用 + 结构化卡片：WS 主路径已透传（ws.py DONE），HTTP 非流式降级路径
    # 此前遗漏导致深度分析卡/卡片不渲染——此处补齐，与 WS 契约对齐（无则 None，null 兼容）。
    last_deep_report: dict[str, object] | None = None
    cards: list[dict[str, object]] | None = None
    # 追问面板（Task 5，2026-08-26）：透出本轮 questions 建议（graph 未采集时为 None，null 兼容）
    questions: list[str] | None = None
