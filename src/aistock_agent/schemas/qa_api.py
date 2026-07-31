"""QA API 请求/响应 schema。"""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class QARequest(BaseModel):
    """POST /api/agent/qa 请求体。"""

    message: str
    thread_id: str | None = None
    constraints: dict[str, str] = {}

    model_config = ConfigDict(extra="forbid")
