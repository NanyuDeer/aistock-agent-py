"""utils.sse 测试 — LangGraph astream_events → SSE 事件映射"""

from unittest.mock import MagicMock

from aistock_agent.constants import SSEEventType
from aistock_agent.utils.sse import map_langgraph_event_to_sse


def test_tool_start_with_query():
    """on_tool_start + 带 query → tool_start 事件带 args"""
    event = {
        "event": "on_tool_start",
        "name": "tavily_finance_search",
        "data": {"input": {"query": "美联储利率"}},
    }
    sse = map_langgraph_event_to_sse(event)
    assert sse == {
        "type": SSEEventType.TOOL_START,
        "tool": "tavily_finance_search",
        "label": "正在搜索财经新闻",
        "args": {"query": "美联储利率"},
    }


def test_tool_start_without_query():
    """on_tool_start + 无 query → tool_start 不带 args 键"""
    event = {
        "event": "on_tool_start",
        "name": "get_global_markets",
        "data": {"input": {}},
    }
    sse = map_langgraph_event_to_sse(event)
    assert sse == {
        "type": SSEEventType.TOOL_START,
        "tool": "get_global_markets",
        "label": "正在获取全球市场行情",
    }
    assert "args" not in sse


def test_tool_start_unknown_tool_uses_name_as_label():
    """未在 TOOL_LABELS 注册的工具，label 回退为工具名本身"""
    event = {
        "event": "on_tool_start",
        "name": "some_unknown_tool",
        "data": {"input": {}},
    }
    sse = map_langgraph_event_to_sse(event)
    assert sse is not None
    assert sse["label"] == "some_unknown_tool"


def test_tool_end():
    event = {"event": "on_tool_end", "name": "get_global_markets", "data": {}}
    sse = map_langgraph_event_to_sse(event)
    assert sse == {"type": SSEEventType.TOOL_END, "tool": "get_global_markets"}


def test_text_chunk_emits_text_event():
    """纯文本 chunk（无 tool_calls）→ text 事件"""
    chunk = MagicMock()
    chunk.content = "今日市场分析"
    chunk.tool_calls = []
    chunk.tool_call_chunks = []
    event = {"event": "on_chat_model_stream", "name": "llm", "data": {"chunk": chunk}}
    assert map_langgraph_event_to_sse(event) == {
        "type": SSEEventType.TEXT,
        "content": "今日市场分析",
    }


def test_tool_call_chunk_filtered():
    """带 tool_calls 的 chunk 应被过滤（返回 None）"""
    chunk = MagicMock()
    chunk.content = "thinking..."
    chunk.tool_calls = [{"name": "get_global_markets"}]
    chunk.tool_call_chunks = []
    event = {"event": "on_chat_model_stream", "name": "llm", "data": {"chunk": chunk}}
    assert map_langgraph_event_to_sse(event) is None


def test_empty_content_chunk_filtered():
    """空 content 的 chunk 应被过滤"""
    chunk = MagicMock()
    chunk.content = ""
    chunk.tool_calls = []
    chunk.tool_call_chunks = []
    event = {"event": "on_chat_model_stream", "name": "llm", "data": {"chunk": chunk}}
    assert map_langgraph_event_to_sse(event) is None


def test_missing_chunk_filtered():
    """data 中无 chunk 键 → None"""
    event = {"event": "on_chat_model_stream", "name": "llm", "data": {}}
    assert map_langgraph_event_to_sse(event) is None


def test_unknown_event_filtered():
    """未识别的 LangGraph 事件 → None（过滤）"""
    event = {"event": "on_chain_start", "name": "chain", "data": {}}
    assert map_langgraph_event_to_sse(event) is None
