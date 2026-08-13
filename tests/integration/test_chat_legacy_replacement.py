"""P1.4 老路径替换 → M5 入口路由切换 — 集成测试。

覆盖（M5 后 /chat/* 恒走 ChatAgent，开关退役）：
- _select_graph() 恒返回 compile_chat_graph()（不读 chat_graph_enabled）
- chat_message 恒走新子图，响应不含已退役字段
- chat_stream_messages 过滤 qa_router 节点
- chat_stream_updates 发射 CHAT 节点 label
- 报告入口（/briefing/*）仍走 compile_graph（回归不破）
"""

import asyncio
import types
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from aistock_agent.api.routes import _select_graph
from aistock_agent.config import settings


@pytest.fixture
def client():
    """构造 FastAPI TestClient，绕过内网鉴权。"""
    from aistock_agent.main import app
    return TestClient(app)


def test_select_graph_always_returns_chat_graph():
    """_select_graph() 恒返回 compile_chat_graph()，不再读 chat_graph_enabled 开关。"""
    with patch(
        "aistock_agent.api.routes.compile_graph",
        side_effect=AssertionError("compile_graph 不应被 chat 入口调用"),
    ), patch("aistock_agent.api.routes.compile_chat_graph") as mock_chat:
        mock_chat.return_value = object()
        graph = _select_graph()
    assert graph is mock_chat.return_value


def test_select_graph_returns_chat_graph_with_qa_router_node():
    """_select_graph 返回真实 ChatAgent，节点包含 qa_router。"""
    graph = _select_graph()
    assert graph is not None
    assert hasattr(graph, "ainvoke")
    assert "qa_router" in graph.nodes


async def _mock_aget_state(config=None):
    """模拟 aget_state 返回空状态。"""
    class _MockState:
        values = {}
    return _MockState()


def test_chat_message_omits_trace_field(client, monkeypatch):
    """/chat/message 恒走新子图，响应不含该字段。"""
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
    assert "advisor_trace" not in data


def test_chat_message_clarification(client):
    """/chat/message 澄清路径返回澄清文本，响应不含该字段。"""
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
    assert "advisor_trace" not in data


@pytest.mark.asyncio
async def test_stream_messages_filters_qa_router_node():
    """_stream_messages 过滤 qa_router 节点的 on_chat_model_stream 事件。

    qa_router 的 LLM 调用是内部意图识别，不应流式给用户。
    """
    from langchain_core.messages import AIMessageChunk

    from aistock_agent.api.routes import _stream_messages
    from aistock_agent.constants import SSEEventType

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
    # 新子图无 analysis_reports（默认值），且响应不含已退役字段
    assert done_events[0]["analysis_reports"] == {}
    assert "advisor_trace" not in done_events[0]


@pytest.mark.asyncio
async def test_stream_updates_emits_label_for_chat_nodes():
    """_stream_updates 在新子图节点切换时发射带 label 的 AGENT_SWITCH 事件。"""
    import asyncio

    from aistock_agent.api.routes import _stream_updates, _update_queues
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


def test_briefing_still_uses_compile_graph(client):
    """/briefing/morning 报告入口仍走 compile_graph（M5 回归不破）。"""
    # 直接验证 morning_briefing 路由内部使用 compile_graph 而非 _select_graph
    # （通过 mock compile_graph 断言被调用；compile_chat_graph 不应被调用）
    with patch("aistock_agent.api.routes.compile_graph") as mock_compile, patch(
        "aistock_agent.api.routes.compile_chat_graph",
        side_effect=AssertionError("报告入口不应走 ChatAgent"),
    ):
        # 触发路由调用（TestClient 走 SSE；这里用 mock 图短路）
        mock_graph = mock_compile.return_value
        mock_graph.astream_events = _empty_stream
        mock_graph.aget_state = _mock_aget_state

        response = client.get(
            "/api/agent/briefing/morning",
            headers={"X-Internal-Token": settings.internal_api_token},
        )
        assert response.status_code == 200
        mock_compile.assert_called()


async def _empty_stream(initial_state, config=None, version="v2"):
    return
    yield  # pragma: no cover


def test_chat_message_force_deep_propagates_to_state(client, monkeypatch):
    """force_deep=true → initial_state['force_deep'] is True（HTTP 对齐 WS）。"""
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "aistock_agent.api.routes.build_chat_initial_state",
        lambda message: captured,
    )

    async def fake_ainvoke(state, config=None):
        return {"final_response": "ok", "insight": None, "trace": None}

    monkeypatch.setattr(
        "aistock_agent.api.routes._select_graph",
        lambda: types.SimpleNamespace(ainvoke=fake_ainvoke),
    )
    resp = client.post(
        "/api/agent/chat/message",
        json={"message": "深度分析一下600519", "force_deep": True},
        headers={"X-Internal-Token": settings.internal_api_token},
    )
    assert resp.status_code == 200
    assert captured["force_deep"] is True


def test_chat_message_force_deep_default_false(client, monkeypatch):
    """未传 force_deep → state 中为 False（缺省）。"""
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "aistock_agent.api.routes.build_chat_initial_state",
        lambda message: captured,
    )

    async def fake_ainvoke(state, config=None):
        return {"final_response": "ok", "insight": None, "trace": None}

    monkeypatch.setattr(
        "aistock_agent.api.routes._select_graph",
        lambda: types.SimpleNamespace(ainvoke=fake_ainvoke),
    )
    resp = client.post(
        "/api/agent/chat/message",
        json={"message": "你好"},
        headers={"X-Internal-Token": settings.internal_api_token},
    )
    assert resp.status_code == 200
    assert captured["force_deep"] is False


def test_chat_stream_messages_force_deep_propagates(client, monkeypatch):
    """SSE 流路径同样透传 force_deep（consumes body 触发 generator）。"""
    captured: dict[str, object] = {}
    monkeypatch.setattr(
        "aistock_agent.api.routes.build_chat_initial_state",
        lambda message: captured,
    )

    async def fake_events(*args, **kwargs):
        return
        yield  # pragma: no cover  # async generator 空流

    async def fake_aget_state(config=None):
        return _mock_aget_state()

    monkeypatch.setattr(
        "aistock_agent.api.routes._select_graph",
        lambda: types.SimpleNamespace(
            astream_events=fake_events, aget_state=fake_aget_state
        ),
    )
    with client.stream(
        "POST",
        "/api/agent/chat/stream/messages",
        json={"message": "深度分析一下600519", "force_deep": True},
        headers={"X-Internal-Token": settings.internal_api_token},
    ) as resp:
        assert resp.status_code == 200
        for _ in resp.iter_lines():
            pass
    assert captured["force_deep"] is True
