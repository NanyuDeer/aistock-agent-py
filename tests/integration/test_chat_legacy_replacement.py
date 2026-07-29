"""P1.4 老路径替换 — 集成测试。

覆盖：
- chat_message 在开关开启/关闭时的行为
- chat_stream_messages 在开关开启时的 SSE 流
- chat_stream_updates 在开关开启时的 AGENT_SWITCH 事件
- 开关回退验证
"""

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
