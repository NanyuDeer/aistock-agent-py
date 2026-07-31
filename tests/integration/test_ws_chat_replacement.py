"""P1.4 风险点处理 — WS 端点集成测试。

覆盖：

- 开关开启时 WS 走新子图（收到新节点 intermediate 事件）
- 开关关闭时 WS 走老路径（收到老节点 intermediate 事件）
- qa_router 的 token 事件被过滤
- 开关开启时 DONE 事件 advisor_trace=null
- 开关回退验证
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aistock_agent.config import settings


@pytest.fixture
def client():
    """构造 FastAPI TestClient。"""
    from aistock_agent.main import app
    return TestClient(app)


@pytest.fixture
def chat_enabled(monkeypatch):
    """临时开启 chat_graph_enabled 开关。"""
    monkeypatch.setattr(settings, "chat_graph_enabled", True)
    yield
    monkeypatch.setattr(settings, "chat_graph_enabled", False)


def test_ws_chat_uses_chat_graph_when_enabled(client, chat_enabled):
    """开关开启时，WS 收到新子图节点的 intermediate 事件。"""
    # mock _select_graph 返回的 graph 的 astream_events
    async def mock_astream_events(initial_state, config=None, version="v2"):
        # 模拟新子图事件流：qa_router 启动 → skill_executor 启动 → synth_answer 启动 → token → end
        yield {
            "event": "on_chain_start",
            "name": "qa_router",
            "data": {},
            "metadata": {"langgraph_node": "qa_router"},
        }
        yield {
            "event": "on_chain_start",
            "name": "skill_executor",
            "data": {},
            "metadata": {"langgraph_node": "skill_executor"},
        }
        yield {
            "event": "on_chain_start",
            "name": "synth_answer",
            "data": {},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        # synth_answer 的 on_chain_end 携带 final_response
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "测试回复"}},
            "metadata": {"langgraph_node": "synth_answer"},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    # Task 1 后 ws.py 通过 `from aistock_agent.api.routes import _select_graph`
    # 引入 _select_graph，故 patch ws 模块内的该名字即可。
    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试问题", "session_id": "test_ws_001"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    # 应收到新子图节点的 intermediate 事件
    intermediate_events = [m for m in messages if m.get("type") == "intermediate"]
    intermediate_nodes = [m.get("node") for m in intermediate_events]
    assert "qa_router" in intermediate_nodes
    assert "skill_executor" in intermediate_nodes
    assert "synth_answer" in intermediate_nodes

    # 应收到 done 事件
    done_events = [m for m in messages if m.get("type") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["content"] == "测试回复"


def test_ws_chat_clarification_when_enabled(client, chat_enabled):
    """开关开启时，澄清路径 WS 返回澄清文本，所有事件 advisor_trace 为 None。"""
    async def mock_astream_events(initial_state, config=None, version="v2"):
        yield {
            "event": "on_chain_start",
            "name": "synth_answer",
            "data": {},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        # synth_answer 的 on_chain_end 携带澄清 final_response
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "请提供 6 位股票代码后重试。"}},
            "metadata": {"langgraph_node": "synth_answer"},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "茅台最近新闻", "session_id": "test_ws_clarification"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    done_events = [m for m in messages if m.get("type") == "done"]
    assert len(done_events) == 1
    assert done_events[0]["content"] == "请提供 6 位股票代码后重试。"
    # 澄清路径无 advisor_trace，所有事件均为 None
    assert all(m.get("advisor_trace") is None for m in messages)


def test_ws_chat_filters_qa_router_tokens(client, chat_enabled):
    """qa_router 的 token 事件被过滤，不发送 text。"""
    async def mock_astream_events(initial_state, config=None, version="v2"):
        # qa_router 的 token（应被过滤）
        qa_chunk = MagicMock()
        qa_chunk.content = "内部意图识别"
        qa_chunk.tool_calls = []
        qa_chunk.tool_call_chunks = []
        yield {
            "event": "on_chat_model_stream",
            "name": "qa_router",
            "data": {"chunk": qa_chunk},
            "metadata": {"langgraph_node": "qa_router"},
        }
        # synth_answer 的 token（应发送）
        synth_chunk = MagicMock()
        synth_chunk.content = "用户可见回复"
        synth_chunk.tool_calls = []
        synth_chunk.tool_call_chunks = []
        yield {
            "event": "on_chat_model_stream",
            "name": "synth_answer",
            "data": {"chunk": synth_chunk},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "用户可见回复"}},
            "metadata": {"langgraph_node": "synth_answer"},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试", "session_id": "test_ws_filter"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    text_events = [m for m in messages if m.get("type") == "text"]
    text_contents = [m.get("content") for m in text_events]
    assert "内部意图识别" not in text_contents
    assert "用户可见回复" in text_contents


def test_ws_chat_done_event_advisor_trace_none_when_enabled(client, chat_enabled):
    """开关开启时，DONE 事件的 advisor_trace=null。"""
    async def mock_astream_events(initial_state, config=None, version="v2"):
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "测试回复"}},
            "metadata": {"langgraph_node": "synth_answer"},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试", "session_id": "test_ws_trace"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    done_events = [m for m in messages if m.get("type") == "done"]
    assert len(done_events) == 1
    # 新子图无 advisor_trace，应为 null
    assert done_events[0]["advisor_trace"] is None


def test_ws_chat_fallback_to_legacy(client, monkeypatch):
    """开关开启后关闭，老路径恢复正常。"""
    # 先开启
    monkeypatch.setattr(settings, "chat_graph_enabled", True)

    async def mock_chat_astream(initial_state, config=None, version="v2"):
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "新子图回复"}},
            "metadata": {"langgraph_node": "synth_answer"},
        }

    mock_chat_graph = MagicMock()
    mock_chat_graph.astream_events = mock_chat_astream

    with patch("aistock_agent.api.ws._select_graph", return_value=mock_chat_graph):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试", "session_id": "test_ws_fb1"})
            data = websocket.receive_json()
            while data.get("type") != "done":
                data = websocket.receive_json()
            assert data["content"] == "新子图回复"
            assert data["advisor_trace"] is None

    # 关闭开关
    monkeypatch.setattr(settings, "chat_graph_enabled", False)

    async def mock_legacy_astream(initial_state, config=None, version="v2"):
        yield {
            "event": "on_chain_start",
            "name": "ai_advisor_agent",
            "data": {},
            "metadata": {"langgraph_node": "ai_advisor_agent"},
        }
        yield {
            "event": "on_chain_end",
            "name": "ai_advisor_agent",
            "data": {
                "output": {
                    "final_response": "老路径回复",
                    "advisor_trace": {
                        "schema_version": "1.0",
                        "subquestions": [],
                        "missing_sources": [],
                        "degraded": False,
                    },
                }
            },
            "metadata": {"langgraph_node": "ai_advisor_agent"},
        }

    mock_legacy_graph = MagicMock()
    mock_legacy_graph.astream_events = mock_legacy_astream

    with patch("aistock_agent.api.ws._select_graph", return_value=mock_legacy_graph):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试", "session_id": "test_ws_fb2"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    done_events = [m for m in messages if m.get("type") == "done"]
    assert done_events[0]["content"] == "老路径回复"
    # 老路径应返回非 null 的 advisor_trace
    assert done_events[0]["advisor_trace"] is not None
    assert done_events[0]["advisor_trace"]["schema_version"] == "1.0"
