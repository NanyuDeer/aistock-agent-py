"""ws /ws/chat 端点回归测试 — checkpointer thread_id config

Task 5 给 ``compile_graph()`` 默认挂载 MemorySaver checkpointer 后，LangGraph 要求
带 checkpointer 的图在 stream/invoke 时必须传
``config={"configurable": {"thread_id": ...}}``，否则抛::

    ValueError: Checkpointer requires one or more of the following 'configurable' keys: []

``routes.py`` 的 ``/chat/message`` 已在 Task 5 同步修复（走 ``ainvoke``），
但 ``ws.py`` 的 ``astream_events`` 调用遗漏，本测试锁定该回归：
验证 ``ws.py`` 调用 ``graph.astream_events`` 时传入了含 ``thread_id`` 的 ``config``。

使用 Starlette ``TestClient.websocket_connect``（同步，无需 ``httpx-ws`` 额外依赖），
mock ``_select_graph`` 返回伪图，捕获 ``astream_events`` 的 ``config`` 入参。
"""
from unittest.mock import patch

from starlette.testclient import TestClient

from aistock_agent.main import app


class _MockGraph:
    """伪图：捕获 astream_events 入参并产出最终节点输出。"""

    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    async def astream_events(
        self, state: dict[str, object], config: object = None, **kwargs: object
    ) -> object:
        self.captured["state"] = state
        self.captured["config"] = config
        yield {"event": "on_chain_end", "data": {"output": {"final_response": "mocked 流式回复"}}}


def test_ws_chat_passes_thread_id_config() -> None:
    """ws.py 调用 graph.astream_events 时必须传 config[configurable][thread_id]"""
    mock_graph = _MockGraph()
    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        client = TestClient(app)
        with client.websocket_connect("/api/agent/ws/chat") as ws:
            ws.send_json({"message": "你好", "session_id": "ws_test_001"})
            agent_msg = ws.receive_json()

    config = mock_graph.captured.get("config")
    assert config is not None, "astream_events 未收到 config（checkpointer 回归未修复）"
    assert config["configurable"]["thread_id"] == "ws_test_001"
    assert agent_msg == {
        "type": "done",
        "content": "mocked 流式回复",
        "last_deep_report": None,  # T4：非 deep 流程 DONE 携带 null
    }


def test_ws_chat_default_session_id_when_missing() -> None:
    """未传 session_id 时，thread_id 回退为 ws_<id> 形式，仍需透传给 astream"""
    mock_graph = _MockGraph()
    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        client = TestClient(app)
        with client.websocket_connect("/api/agent/ws/chat") as ws:
            ws.send_json({"message": "你好"})
            ws.receive_json()  # done

    config = mock_graph.captured.get("config")
    assert config is not None
    thread_id = config["configurable"]["thread_id"]
    assert isinstance(thread_id, str)
    assert thread_id.startswith("ws_")


def test_ws_done_omits_trace_field() -> None:
    """DONE 事件结构回归：不含已退役字段。"""
    mock_graph = _MockGraph()
    with patch("aistock_agent.api.ws._select_graph", return_value=mock_graph):
        client = TestClient(app)
        with client.websocket_connect("/api/agent/ws/chat") as ws:
            ws.send_json({"message": "个股 600519"})
            done = ws.receive_json()

    assert done == {
        "type": "done",
        "content": "mocked 流式回复",
        "last_deep_report": None,  # T4：非 deep 流程 DONE 携带 null
    }
    assert "advisor_trace" not in done
