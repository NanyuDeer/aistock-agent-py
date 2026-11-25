"""schemas 测试 — ChatRequest/ChatResponse/SSEEvent/AgentInput/AgentOutput"""

import pytest
from pydantic import ValidationError

from aistock_agent.constants import SSEEventType
from aistock_agent.schemas.agents import AgentInput, AgentOutput
from aistock_agent.schemas.chat import ChatRequest, ChatResponse
from aistock_agent.schemas.sse import SSEEvent

# ── ChatRequest / ChatResponse ─────────────────────────────────────


def test_chat_request_valid():
    req = ChatRequest(message="分析 600519")
    assert req.message == "分析 600519"
    assert req.favorites == []
    assert req.session_id is None


def test_chat_request_empty_message_rejected():
    """message 字段非空校验"""
    with pytest.raises(ValidationError):
        ChatRequest(message="")


def test_chat_request_with_all_fields():
    req = ChatRequest(
        message="你好",
        session_id="s1",
        user_id="u1",
        favorites=["600519"],
    )
    assert req.session_id == "s1"
    assert req.favorites == ["600519"]


def test_chat_response():
    resp = ChatResponse(content="回复", session_id="s1")
    assert resp.content == "回复"
    assert resp.session_id == "s1"


# ── SSEEvent ───────────────────────────────────────────────────────


def test_sse_event_valid_types():
    for t in [
        SSEEventType.TOOL_START,
        SSEEventType.TOOL_END,
        SSEEventType.LLM_START,
        SSEEventType.TEXT,
        SSEEventType.DONE,
        SSEEventType.ERROR,
    ]:
        evt = SSEEvent(type=t)
        assert evt.type == t


def test_sse_event_invalid_type_rejected():
    """type 取值必须在 SSEEventType 集合内"""
    with pytest.raises(ValidationError):
        SSEEvent(type="unknown_event")


# ── AgentInput / AgentOutput（文档化模型）──────────────────────────


def test_agent_input_defaults():
    inp = AgentInput()
    assert inp.messages == []
    assert inp.favorites == []


def test_agent_output_defaults():
    out = AgentOutput()
    assert out.final_response is None
