"""P1.4 风险点处理 → M5 入口路由切换 — WS 端点集成测试。

覆盖（M5 后 /ws/chat 恒走 ChatAgent，开关退役）：
- WS 恒用 build_chat_initial_state 构造状态（收到新子图节点 intermediate 事件）
- qa_router 的 token 事件被过滤
- DONE 事件不含已退役字段
- 入口解析字段：user_id 已透传到 state（D11），favorites 保留但不传入 state
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from aistock_agent.services.chat_task_manager import chat_task_manager


@pytest.fixture
def client():
    """构造 FastAPI TestClient。"""
    from aistock_agent.main import app
    return TestClient(app)


def _assert_chat_initial_state(initial_state, expected_user_id=None):
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
    assert "favorites" not in initial_state
    assert "trigger_source" not in initial_state
    # D11：user_id 已透传到 QuestionState（payload 无 user_id 时为 None）
    assert initial_state.get("user_id") == expected_user_id
    # T6：deep_source/final_response 是单轮 transient 信号，ws.py 每轮重置为 None
    assert initial_state.get("deep_source") is None
    assert initial_state.get("final_response") is None


def test_ws_chat_uses_chat_graph(client):
    """WS 恒走 ChatAgent：收到新子图节点 intermediate 事件 + done。"""
    async def mock_astream_events(initial_state, config=None, version="v2"):
        # payload 带 user_id → D11 已透传到 state
        _assert_chat_initial_state(initial_state, expected_user_id="user_1")
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

    with (
        patch("aistock_agent.api.ws._select_graph", return_value=mock_graph),
        patch("aistock_agent.api.ws.stream_reasoning", new=AsyncMock()),
    ):
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
    """澄清路径 WS 返回澄清文本，所有事件不含该字段。"""
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

    with (
        patch("aistock_agent.api.ws._select_graph", return_value=mock_graph),
        patch("aistock_agent.api.ws.stream_reasoning", new=AsyncMock()),
    ):
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
    # 澄清路径所有事件均不含该字段
    assert all("advisor_trace" not in m for m in messages)


def test_ws_chat_filters_qa_router_tokens(client):
    """qa_router / synth_answer 的 token 事件被过滤；非过滤节点（escalate）正常发送。"""
    async def mock_astream_events(initial_state, config=None, version="v2"):
        def stream(name: str, content: str) -> dict:
            chunk = MagicMock()
            chunk.content = content
            chunk.tool_calls = []
            chunk.tool_call_chunks = []
            return {"event": "on_chat_model_stream", "name": name,
                    "data": {"chunk": chunk}, "metadata": {"langgraph_node": name}}
        # P3-fix-2 T1.2：on_chain_start 先置 current_node，再流 token
        yield {"event": "on_chain_start", "name": "qa_router", "data": {},
               "metadata": {"langgraph_node": "qa_router"}}
        yield stream("ChatOpenAI", "内部意图识别")    # qa_router 的 JSON → 过滤
        yield {"event": "on_chain_start", "name": "escalate", "data": {},
               "metadata": {"langgraph_node": "escalate"}}
        yield stream("ChatOpenAI", "用户可见回复")    # escalate 的 worker 流 → 转发
        yield {"event": "on_chain_start", "name": "synth_answer", "data": {},
               "metadata": {"langgraph_node": "synth_answer"}}
        yield stream("ChatOpenAI", "综合结论 JSON")   # synth_answer 的 JSON → 过滤
        yield {"event": "on_chain_end", "name": "synth_answer",
               "data": {"output": {"final_response": "用户可见回复"}},
               "metadata": {"langgraph_node": "synth_answer"}}

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    with (
        patch("aistock_agent.api.ws._select_graph", return_value=mock_graph),
        # 修复后 DONE 前会 drain reasoning task，必须 patch 避免真实 LLM 调用
        patch("aistock_agent.api.ws.stream_reasoning", new=AsyncMock()),
    ):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试", "session_id": "test_ws_filter"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    text_contents = [m.get("content") for m in messages if m.get("type") == "text"]
    assert "内部意图识别" not in text_contents
    assert "综合结论 JSON" not in text_contents
    assert "用户可见回复" in text_contents


def test_ws_chat_done_omits_trace_field(client):
    """DONE 事件不含该字段。"""
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
    # 新子图无该字段
    assert "advisor_trace" not in done_events[0]


def test_ws_chat_forwards_content_delta(client):
    """改进 17（Task 1）：on_custom_event 捕获 chat_content_delta → content_delta 事件。

    - 逐段增量经统一 sink 转发（顺序保持）
    - 未知自定义事件名静默忽略（事件名冻结，硬约束 5）
    - 事件入 state.events（resume 回放兼容，硬约束 4）
    """
    async def mock_astream_events(initial_state, config=None, version="v2"):
        _assert_chat_initial_state(initial_state)
        yield {
            "event": "on_chain_start",
            "name": "synth_answer",
            "data": {},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        yield {
            "event": "on_custom_event",
            "name": "chat_content_delta",
            "data": {"content": "第一段增量"},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        yield {
            "event": "on_custom_event",
            "name": "chat_content_delta",
            "data": {"content": "第二段增量"},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        # 未知事件名：不转发、不报错（与分支只匹配两个冻结名对齐）
        yield {
            "event": "on_custom_event",
            "name": "some_unrelated_event",
            "data": {"content": "不应出现在 WS 流中"},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "第一段增量第二段增量"}},
            "metadata": {"langgraph_node": "synth_answer"},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    with (
        patch("aistock_agent.api.ws._select_graph", return_value=mock_graph),
        patch("aistock_agent.api.ws.stream_reasoning", new=AsyncMock()),
    ):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试", "session_id": "test_ws_content_delta"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    delta_events = [m for m in messages if m.get("type") == "content_delta"]
    assert [m.get("content") for m in delta_events] == ["第一段增量", "第二段增量"]
    # 未知事件名不转发
    assert all("不应出现在 WS 流中" not in str(m) for m in messages)
    # 事件经统一 sink 入 state.events（resume 回放兼容，硬约束 4）
    state = chat_task_manager.get("test_ws_content_delta")
    assert state is not None
    state_delta = [e for e in state.events if e.get("type") == "content_delta"]
    assert [e.get("content") for e in state_delta] == ["第一段增量", "第二段增量"]


def test_ws_chat_forwards_content_reset(client):
    """改进 17（Task 1）：on_custom_event 捕获 chat_content_reset → content_reset 显式整段替换。

    D4 语义：结构化校验失败时前端需整段覆盖半截流式内容；事件入 state.events。
    """
    async def mock_astream_events(initial_state, config=None, version="v2"):
        _assert_chat_initial_state(initial_state)
        yield {
            "event": "on_chain_start",
            "name": "synth_answer",
            "data": {},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        yield {
            "event": "on_custom_event",
            "name": "chat_content_delta",
            "data": {"content": "半截流式内容"},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        yield {
            "event": "on_custom_event",
            "name": "chat_content_reset",
            "data": {"content": "降级后的完整回答"},
            "metadata": {"langgraph_node": "synth_answer"},
        }
        yield {
            "event": "on_chain_end",
            "name": "synth_answer",
            "data": {"output": {"final_response": "降级后的完整回答"}},
            "metadata": {"langgraph_node": "synth_answer"},
        }

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    with (
        patch("aistock_agent.api.ws._select_graph", return_value=mock_graph),
        patch("aistock_agent.api.ws.stream_reasoning", new=AsyncMock()),
    ):
        with client.websocket_connect("/api/agent/ws/chat") as websocket:
            websocket.send_json({"message": "测试", "session_id": "test_ws_content_reset"})
            messages = []
            while True:
                data = websocket.receive_json()
                messages.append(data)
                if data.get("type") == "done":
                    break

    reset_events = [m for m in messages if m.get("type") == "content_reset"]
    assert len(reset_events) == 1
    assert reset_events[0]["content"] == "降级后的完整回答"
    # 事件经统一 sink 入 state.events（resume 回放兼容，硬约束 4）
    state = chat_task_manager.get("test_ws_content_reset")
    assert state is not None
    state_reset = [e for e in state.events if e.get("type") == "content_reset"]
    assert len(state_reset) == 1
    assert state_reset[0]["content"] == "降级后的完整回答"
