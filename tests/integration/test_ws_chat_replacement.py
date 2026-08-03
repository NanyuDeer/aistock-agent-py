"""P1.4 风险点处理 → M5 入口路由切换 — WS 端点集成测试。

覆盖（M5 后 /ws/chat 恒走 ChatAgent，开关退役）：
- WS 恒用 build_chat_initial_state 构造状态（收到新子图节点 intermediate 事件）
- qa_router 的 token 事件被过滤
- DONE 事件 advisor_trace=null
- 入口解析字段（user_id / favorites）保留但不传入 state
"""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    """构造 FastAPI TestClient。"""
    from aistock_agent.main import app
    return TestClient(app)


def _assert_chat_initial_state(initial_state):
    """断言 WS 传入的是 build_chat_initial_state 构造的 QuestionState
    （而非老路径完整 AgentState）。
    """
    assert isinstance(initial_state, dict)
    assert "goal" in initial_state
    assert "skill_calls" in initial_state
    assert "evidences" in initial_state
    assert "clarification" in initial_state
    # 老路径专属字段不应出现
    assert "session_id" not in initial_state
    assert "user_id" not in initial_state
    assert "favorites" not in initial_state
    assert "trigger_source" not in initial_state


def test_ws_chat_uses_chat_graph(client):
    """WS 恒走 ChatAgent：收到新子图节点 intermediate 事件 + done。"""
    async def mock_astream_events(initial_state, config=None, version="v2"):
        _assert_chat_initial_state(initial_state)
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

    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({
                "message": "测试问题",
                "session_id": "test_ws_001",
                "user_id": "user_1",
                "favorites": ["600519"],
            })
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


def test_ws_chat_clarification(client):
    """澄清路径 WS 返回澄清文本，所有事件 advisor_trace 为 None。"""
    async def mock_astream_events(initial_state, config=None, version="v2"):
        _assert_chat_initial_state(initial_state)
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


def test_ws_chat_filters_qa_router_tokens(client):
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


def test_ws_chat_done_event_advisor_trace_none(client):
    """DONE 事件的 advisor_trace=null。"""
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
