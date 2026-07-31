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


def test_chat_message_clarification_when_enabled(client, chat_enabled):
    """开关开启时，/chat/message 澄清路径返回澄清文本。"""
    async def mock_ainvoke(state, config=None):
        return {
            "final_response": "请提供 6 位股票代码后重试。",
            "insight": None,
            "trace": None,
        }

    with patch("aistock_agent.api.routes.compile_chat_graph") as mock_compile:
        mock_graph = mock_compile.return_value
        mock_graph.ainvoke = mock_ainvoke
        mock_graph.aget_state = _mock_aget_state

        response = client.post(
            "/api/agent/chat/message",
            json={"message": "茅台最近新闻"},
            headers={"X-Internal-Token": settings.internal_api_token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "请提供 6 位股票代码后重试。"
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


@pytest.mark.asyncio
async def test_stream_updates_emits_label_for_chat_nodes(chat_enabled):
    """_stream_updates 在新子图节点切换时发射带 label 的 AGENT_SWITCH 事件。"""
    import asyncio
    from aistock_agent.api.routes import (
        _stream_updates,
        _message_queues,
        _update_queues,
    )
    from aistock_agent.constants import CHAT_NODE_LABELS, SSEEventType

    # 构造事件序列：qa_router → skill_executor → synth_answer → None 哨兵
    events = [
        {
            "event": "on_chain_start",
            "name": "qa_router",
            "metadata": {"langgraph_node": "qa_router"},
            "data": {},
        },
        {
            "event": "on_chain_start",
            "name": "skill_executor",
            "metadata": {"langgraph_node": "skill_executor"},
            "data": {},
        },
        {
            "event": "on_chain_start",
            "name": "synth_answer",
            "metadata": {"langgraph_node": "synth_answer"},
            "data": {},
        },
        None,
    ]

    session_id = "test_session_updates_label"
    _update_queues[session_id] = asyncio.Queue()
    for ev in events:
        await _update_queues[session_id].put(ev)

    sse_events = []
    async for sse in _stream_updates(session_id):
        sse_events.append(sse)

    agent_switches = [e for e in sse_events if e.get("type") == SSEEventType.AGENT_SWITCH]
    assert len(agent_switches) == 3

    # 验证每个 AGENT_SWITCH 包含 label 字段，且与 CHAT_NODE_LABELS 一致
    assert agent_switches[0]["to_node"] == "qa_router"
    assert agent_switches[0]["label"] == CHAT_NODE_LABELS["qa_router"]

    assert agent_switches[1]["to_node"] == "skill_executor"
    assert agent_switches[1]["label"] == CHAT_NODE_LABELS["skill_executor"]

    assert agent_switches[2]["to_node"] == "synth_answer"
    assert agent_switches[2]["label"] == CHAT_NODE_LABELS["synth_answer"]

    # DONE 事件
    done_events = [e for e in sse_events if e.get("type") == SSEEventType.DONE]
    assert len(done_events) == 1


def test_chat_message_legacy_path_when_disabled(client, chat_disabled):
    """开关关闭时，/chat/message 走老路径，advisor_trace 来自老路径结果。"""
    # mock compile_graph 返回的 graph 的 ainvoke
    async def mock_ainvoke(state, config=None):
        return {
            "final_response": "老路径回复",
            "advisor_trace": {
                "schema_version": "1.0",
                "subquestions": [],
                "missing_sources": [],
                "degraded": False,
            },
        }

    with patch("aistock_agent.api.routes.compile_graph") as mock_compile:
        mock_graph = mock_compile.return_value
        mock_graph.ainvoke = mock_ainvoke

        response = client.post(
            "/api/agent/chat/message",
            json={"message": "测试问题"},
            headers={"X-Internal-Token": settings.internal_api_token},
        )

    assert response.status_code == 200
    data = response.json()
    assert data["content"] == "老路径回复"
    # 老路径应返回非 None 的 advisor_trace
    assert data["advisor_trace"] is not None
    assert data["advisor_trace"]["schema_version"] == "1.0"


def test_fallback_to_legacy_after_disabling(client, monkeypatch):
    """开关开启后关闭，老路径恢复正常。"""
    # 先开启
    monkeypatch.setattr(settings, "chat_graph_enabled", True)

    async def mock_chat_ainvoke(state, config=None):
        return {"final_response": "新子图回复", "insight": None, "trace": None}

    with patch("aistock_agent.api.routes.compile_chat_graph") as mock_chat_compile:
        mock_chat_graph = mock_chat_compile.return_value
        mock_chat_graph.ainvoke = mock_chat_ainvoke

        response = client.post(
            "/api/agent/chat/message",
            json={"message": "测试"},
            headers={"X-Internal-Token": settings.internal_api_token},
        )
        assert response.status_code == 200
        assert response.json()["content"] == "新子图回复"
        assert response.json()["advisor_trace"] is None

    # 关闭开关
    monkeypatch.setattr(settings, "chat_graph_enabled", False)

    async def mock_legacy_ainvoke(state, config=None):
        return {
            "final_response": "老路径回复",
            "advisor_trace": {
                "schema_version": "1.0",
                "subquestions": [],
                "missing_sources": [],
                "degraded": False,
            },
        }

    with patch("aistock_agent.api.routes.compile_graph") as mock_legacy_compile:
        mock_legacy_graph = mock_legacy_compile.return_value
        mock_legacy_graph.ainvoke = mock_legacy_ainvoke

        response = client.post(
            "/api/agent/chat/message",
            json={"message": "测试"},
            headers={"X-Internal-Token": settings.internal_api_token},
        )
        assert response.status_code == 200
        assert response.json()["content"] == "老路径回复"
        assert response.json()["advisor_trace"] is not None
