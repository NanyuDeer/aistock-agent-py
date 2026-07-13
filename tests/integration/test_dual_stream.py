"""双流 SSE 端点集成测试

验证双 Queue fan-out 架构的核心行为：
- messages 流（filter_type="text"）：仅 TEXT + LLM_START + DONE
- updates 流（filter_type="tool"）：仅 TOOL_START/END + AGENT_SWITCH + DONE
- graph 仅由 messages 首次连接触发执行一次
- 异常传播：graph 失败时两条流都收到 error 事件
- 队列清理：流结束后 message/update queue 被清理
- 双队列隔离：messages 和 updates 各自拥有独立 Queue，互不竞争
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from aistock_agent.constants import SSEEventType


# ── 辅助：构造 mock 事件 ──


def _make_chunk(content: str) -> MagicMock:
    """构造 LangChain chunk mock（纯文本，无 tool_calls）"""
    chunk = MagicMock()
    chunk.content = content
    chunk.tool_calls = []
    chunk.tool_call_chunks = []
    return chunk


def _tool_start_event(tool_name: str, node: str = "stock_analyst") -> dict:
    return {
        "event": "on_tool_start",
        "name": tool_name,
        "data": {"input": {"symbol": "600519"}},
        "metadata": {"langgraph_node": node},
    }


def _tool_end_event(tool_name: str, node: str = "stock_analyst") -> dict:
    return {
        "event": "on_tool_end",
        "name": tool_name,
        "data": {},
        "metadata": {"langgraph_node": node},
    }


def _text_event(content: str, node: str = "stock_analyst") -> dict:
    return {
        "event": "on_chat_model_stream",
        "name": "llm",
        "data": {"chunk": _make_chunk(content)},
        "metadata": {"langgraph_node": node},
    }


def _cleanup_queues(routes_mod, session_id: str) -> None:
    """清理双队列，避免跨测试残留。"""
    routes_mod._message_queues.pop(session_id, None)
    routes_mod._update_queues.pop(session_id, None)


# ── 测试双 Queue fan-out：单次 graph 执行，两条流各取所需 ──


@pytest.mark.asyncio
async def test_queue_fanout_single_graph_execution():
    """messages 和 updates 共享同一次 graph 执行，不会重复调用 astream_events。

    messages 流触发 graph 执行（call_count=1），每个事件同时推入 message queue
    和 update queue。updates 流从独立 update queue 读取，获得全部事件后过滤工具事件。
    """
    from aistock_agent.api import routes as routes_mod

    call_count = 0

    async def mock_astream_events(initial_state, **kw):
        nonlocal call_count
        call_count += 1
        # 模拟一组事件：tool 调用 → text 输出
        yield _tool_start_event("get_quote")
        yield _tool_end_event("get_quote")
        yield _text_event("茅台当前价格")

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events

    # mock aget_state 返回 final
    mock_final = MagicMock()
    mock_final.values = {"final_response": "茅台分析完成", "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)

    session_id = "test-fanout"
    initial_state = {"messages": [], "session_id": session_id}

    _cleanup_queues(routes_mod, session_id)

    # ── messages 流：触发 graph 执行 ──
    msg_events = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]

    # graph 只执行了一次
    assert call_count == 1

    # messages 流：不含 tool 事件，含 text + done
    msg_types = [e["type"] for e in msg_events]
    assert SSEEventType.TOOL_START not in msg_types
    assert SSEEventType.TOOL_END not in msg_types
    assert SSEEventType.TEXT in msg_types
    assert SSEEventType.DONE in msg_types

    # done 事件携带 final_response
    done_event = msg_events[-1]
    assert done_event["type"] == SSEEventType.DONE
    assert done_event["final_response"] == "茅台分析完成"

    # 清理（_run_graph_to_queue 的 finally 已清理，但确保无残留）
    _cleanup_queues(routes_mod, session_id)

    # ── updates 流：预填充 update 队列，验证过滤逻辑 ──
    # （在生产中，updates 与 messages 各自独立消费同一组事件的副本；
    #   测试中 messages 已消费完毕且队列已清理，故预填充 update queue 验证过滤行为）
    queue = routes_mod._ensure_update_queue(session_id)
    await queue.put(_tool_start_event("get_quote"))
    await queue.put(_tool_end_event("get_quote"))
    await queue.put(_text_event("茅台当前价格"))
    await queue.put(None)  # 哨兵

    upd_events = [e async for e in routes_mod._stream_updates(session_id)]

    # updates 流：不含 text 事件，含 tool + agent_switch + done
    upd_types = [e["type"] for e in upd_events]
    assert SSEEventType.TEXT not in upd_types
    assert SSEEventType.TOOL_START in upd_types
    assert SSEEventType.TOOL_END in upd_types
    assert SSEEventType.AGENT_SWITCH in upd_types
    assert SSEEventType.DONE in upd_types

    # 清理
    _cleanup_queues(routes_mod, session_id)


@pytest.mark.asyncio
async def test_queue_fanout_error_propagation():
    """graph 执行异常时，两条流都收到 error 事件。"""
    from aistock_agent.api import routes as routes_mod

    async def mock_astream_events_error(initial_state, **kw):
        raise RuntimeError("graph failure")
        yield  # 使函数成为 async generator（实际不会执行到这里）

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events_error

    session_id = "test-error-fanout"
    initial_state = {"messages": [], "session_id": session_id}

    _cleanup_queues(routes_mod, session_id)

    # ── messages 流：graph 异常 → error 事件 ──
    msg_events = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]

    assert msg_events[0]["type"] == SSEEventType.ERROR
    assert "graph failure" in msg_events[0]["message"]

    # 清理
    _cleanup_queues(routes_mod, session_id)

    # ── updates 流：预填充 error 事件，验证 error 传播 ──
    queue = routes_mod._ensure_update_queue(session_id)
    await queue.put({"__error__": "graph failure"})
    await queue.put(None)  # 哨兵（_run_graph_to_queue 的 finally 会 put None）

    upd_events = [e async for e in routes_mod._stream_updates(session_id)]

    assert upd_events[0]["type"] == SSEEventType.ERROR
    assert "graph failure" in upd_events[0]["message"]

    _cleanup_queues(routes_mod, session_id)


@pytest.mark.asyncio
async def test_queue_cleanup_after_stream_complete():
    """流结束后 message/update queue 被清理，不泄漏内存。"""
    from aistock_agent.api import routes as routes_mod

    async def mock_astream_events(initial_state, **kw):
        yield _text_event("test", node="general_agent")

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events
    mock_final = MagicMock()
    mock_final.values = {"final_response": "test", "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)

    session_id = "test-cleanup"
    initial_state = {"messages": [], "session_id": session_id}

    _cleanup_queues(routes_mod, session_id)

    _ = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]

    # message queue 已被 _run_graph_to_queue 的 finally 块清理
    assert session_id not in routes_mod._message_queues
    assert session_id not in routes_mod._update_queues


@pytest.mark.asyncio
async def test_messages_stream_no_updates_connection():
    """仅 messages 连接时（无 updates 连接），正常工作。"""
    from aistock_agent.api import routes as routes_mod

    async def mock_astream_events(initial_state, **kw):
        yield _text_event("独立消息流测试", node="general_agent")

    mock_graph = MagicMock()
    mock_graph.astream_events = mock_astream_events
    mock_final = MagicMock()
    mock_final.values = {"final_response": "独立消息流测试", "analysis_reports": {}}
    mock_graph.aget_state = AsyncMock(return_value=mock_final)

    session_id = "test-solo-messages"
    initial_state = {"messages": [], "session_id": session_id}

    _cleanup_queues(routes_mod, session_id)

    events = [e async for e in routes_mod._stream_messages(mock_graph, initial_state, session_id)]

    text_events = [e for e in events if e["type"] == SSEEventType.TEXT]
    assert len(text_events) == 1
    assert text_events[0]["content"] == "独立消息流测试"
    assert events[-1]["type"] == SSEEventType.DONE

    _cleanup_queues(routes_mod, session_id)
