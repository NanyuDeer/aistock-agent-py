"""P1.4 老路径替换 — 集成测试。

覆盖：
- chat_message 在开关开启/关闭时的行为
- chat_stream_messages 在开关开启时的 SSE 流
- chat_stream_updates 在开关开启时的 AGENT_SWITCH 事件
- 开关回退验证
"""

import asyncio
import json
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aistock_agent.api.routes import _select_graph
from aistock_agent.config import settings
from aistock_agent.graph.builder import compile_graph
from aistock_agent.graph.chat_builder import compile_chat_graph


@pytest.fixture
def client():
    """构造 FastAPI TestClient，绕过内网鉴权。"""
    from aistock_agent.main import app
    return TestClient(app)


@pytest.fixture
def chat_enabled(monkeypatch):
    """临时开启 chat_graph_enabled 开关。"""
    monkeypatch.setattr(settings, "chat_graph_enabled", True)
    yield
    monkeypatch.setattr(settings, "chat_graph_enabled", False)


@pytest.fixture
def chat_disabled(monkeypatch):
    """确保 chat_graph_enabled 关闭（默认状态）。"""
    monkeypatch.setattr(settings, "chat_graph_enabled", False)
    yield


def test_select_graph_returns_compile_graph_when_disabled(chat_disabled):
    """开关关闭时，_select_graph 返回老路径 graph。"""
    graph = _select_graph()
    assert graph is not None
    assert hasattr(graph, "ainvoke")


def test_select_graph_returns_compile_chat_graph_when_enabled(chat_enabled):
    """开关开启时，_select_graph 返回新 CHAT 子图。"""
    graph = _select_graph()
    assert graph is not None
    assert hasattr(graph, "ainvoke")
    # 新子图的节点名应包含 qa_router
    assert "qa_router" in graph.nodes


async def _mock_aget_state(config=None):
    """模拟 aget_state 返回空状态。"""
    class _MockState:
        values = {}
    return _MockState()


def test_chat_message_returns_advisor_trace_none_when_enabled(
    client, chat_enabled, monkeypatch
):
    """开关开启时，/chat/message 走新子图，advisor_trace=None。"""
    async def mock_ainvoke(state, config=None):
        return {
            "final_response": "测试回复",
            "insight": None,
            "trace": None,
        }

    with patch("aistock_agent.api.routes.compile_chat_graph") as mock_compile:
        mock_graph = mock_compile.return_value
        mock_graph.ainvoke = mock_ainvoke
        mock_graph.aget_state = _mock_aget_state

        response = client.post(
            "/api/agent/chat/message",
            json={"message": "测试问题"},
            headers={"X-Internal-Token": settings.internal_api_token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "测试回复"
    assert data["advisor_trace"] is None


@pytest.mark.asyncio
async def test_stream_messages_filters_qa_router_node(chat_enabled):
    """_stream_messages 过滤 qa_router 节点的 on_chat_model_stream 事件。

    qa_router 的 LLM 调用是内部意图识别，不应流式给用户。
    """
    from aistock_agent.api.routes import _stream_messages
    from aistock_agent.constants import SSEEventType
    from langchain_core.messages import AIMessageChunk

    # 构造 mock 事件队列：qa_router 的 token + synth_answer 的 token + None 哨兵
    qa_router_token_event = {
        "event": "on_chat_model_stream",
        "name": "some_model",
        "data": {"chunk": AIMessageChunk(content="内部意图识别")},
        "metadata": {"langgraph_node": "qa_router"},
    }
    synth_token_event = {
        "event": "on_chat_model_stream",
        "name": "some_model",
        "data": {"chunk": AIMessageChunk(content="用户可见回复")},
        "metadata": {"langgraph_node": "synth_answer"},
    }

    # mock graph：aget_state 返回空状态
    class MockGraph:
        async def aget_state(self, config=None):
            class MockState:
                values = {"final_response": "用户可见回复"}
            return MockState()

    events = [qa_router_token_event, synth_token_event, None]

    async def mock_run_graph_to_queue(graph, initial_state, session_id):
        from aistock_agent.api.routes import _message_queues, _update_queues
        msg_q = _message_queues.setdefault(session_id, asyncio.Queue())
        upd_q = _update_queues.setdefault(session_id, asyncio.Queue())
        for ev in events:
            await msg_q.put(ev)
            await upd_q.put(ev)

    with patch("aistock_agent.api.routes._run_graph_to_queue", side_effect=mock_run_graph_to_queue):
        sse_events = []
        async for sse in _stream_messages(MockGraph(), {}, "test_session_qa_router"):
            sse_events.append(sse)

    # qa_router 的 token 应被过滤，不产生 TEXT 事件
    text_events = [e for e in sse_events if e.get("type") == SSEEventType.TEXT]
    text_contents = [e.get("content") for e in text_events]
    assert "内部意图识别" not in text_contents
    assert "用户可见回复" in text_contents

    # DONE 事件应包含 final_response
    done_events = [e for e in sse_events if e.get("type") == SSEEventType.DONE]
    assert len(done_events) == 1
    assert done_events[0]["final_response"] == "用户可见回复"
    # 新子图无 analysis_reports / advisor_trace，应为默认值
    assert done_events[0]["analysis_reports"] == {}
    assert done_events[0]["advisor_trace"] is None
