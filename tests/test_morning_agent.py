"""morning_agent 测试"""
import pytest
from datetime import date

from aistock_agent.agents.morning_agent import is_trading_day


def test_is_trading_day_weekday():
    # 2026-07-06 是周一
    assert is_trading_day(date(2026, 7, 6)) is True


def test_is_trading_day_saturday():
    # 2026-07-04 是周六
    assert is_trading_day(date(2026, 7, 4)) is False


def test_is_trading_day_national_holiday():
    # 2026-10-01 是国庆节
    assert is_trading_day(date(2026, 10, 1)) is False


def test_is_trading_day_no_arg_returns_bool():
    # 不传参数时调用 date.today()，验证不崩溃且返回 bool
    result = is_trading_day()
    assert isinstance(result, bool)


# ── stream() 测试 ──────────────────────────────────────────────────
from unittest.mock import AsyncMock, MagicMock, patch

from aistock_agent.agents import morning_agent


async def _async_iter(items):
    """将列表转换为异步迭代器，用于 mock astream_events"""
    for item in items:
        yield item


@pytest.mark.asyncio
async def test_stream_cache_hit(mock_redis):
    """缓存命中：只 yield text + done，不调用 LLM"""
    mock_redis.get.return_value = b"cached briefing content"

    events = [e async for e in morning_agent.stream({})]

    assert events == [
        {"type": "text", "content": "cached briefing content"},
        {"type": "done"},
    ]


@pytest.mark.asyncio
async def test_stream_tool_events_mapped(mock_redis):
    """tool_start/tool_end 正确映射标签，tavily 带 args"""
    mock_redis.get.return_value = None
    mock_redis.setex = AsyncMock()

    raw_events = [
        {"event": "on_tool_start", "name": "get_global_markets",
         "data": {"input": {}}},
        {"event": "on_tool_end", "name": "get_global_markets", "data": {}},
        {"event": "on_tool_start", "name": "tavily_finance_search",
         "data": {"input": {"query": "美联储利率"}}},
        {"event": "on_tool_end", "name": "tavily_finance_search", "data": {}},
    ]

    mock_agent = MagicMock()
    mock_agent.astream_events = lambda *a, **kw: _async_iter(raw_events)

    with patch("aistock_agent.agents.morning_agent.create_react_agent",
               return_value=mock_agent):
        with patch("aistock_agent.agents.morning_agent.is_trading_day",
                   return_value=True):
            events = [e async for e in morning_agent.stream({})]

    assert {"type": "tool_start", "tool": "get_global_markets",
            "label": "正在获取全球市场行情"} in events
    assert {"type": "tool_end", "tool": "get_global_markets"} in events
    assert {"type": "tool_start", "tool": "tavily_finance_search",
            "label": "正在搜索财经新闻",
            "args": {"query": "美联储利率"}} in events
    assert events[-1] == {"type": "done"}


@pytest.mark.asyncio
async def test_stream_filters_tool_call_chunks(mock_redis):
    """带 tool_calls 的 chunk 不产生 text 事件，纯文本 chunk 正常 yield"""
    mock_redis.get.return_value = None
    mock_redis.setex = AsyncMock()

    tool_chunk = MagicMock()
    tool_chunk.content = "thinking..."
    tool_chunk.tool_calls = [{"name": "get_global_markets"}]
    tool_chunk.tool_call_chunks = []

    text_chunk = MagicMock()
    text_chunk.content = "今日市场分析"
    text_chunk.tool_calls = []
    text_chunk.tool_call_chunks = []

    raw_events = [
        {"event": "on_chat_model_stream", "name": "llm",
         "data": {"chunk": tool_chunk}},
        {"event": "on_chat_model_stream", "name": "llm",
         "data": {"chunk": text_chunk}},
    ]

    mock_agent = MagicMock()
    mock_agent.astream_events = lambda *a, **kw: _async_iter(raw_events)

    with patch("aistock_agent.agents.morning_agent.create_react_agent",
               return_value=mock_agent):
        with patch("aistock_agent.agents.morning_agent.is_trading_day",
                   return_value=True):
            events = [e async for e in morning_agent.stream({})]

    text_events = [e for e in events if e.get("type") == "text"]
    assert len(text_events) == 1
    assert text_events[0]["content"] == "今日市场分析"
    assert {"type": "llm_start", "label": "正在生成分析报告"} in events


@pytest.mark.asyncio
async def test_stream_non_trading_day_injects_prompt(mock_redis):
    """非交易日时 system prompt 包含非交易日提示"""
    mock_redis.get.return_value = None
    mock_redis.setex = AsyncMock()
    captured: dict = {}

    def fake_create(llm, tools):
        mock_inner = MagicMock()

        async def fake_astream(inp, **kw):
            captured.update(inp)
            return
            yield  # 使其成为 async generator

        mock_inner.astream_events = fake_astream
        return mock_inner

    with patch("aistock_agent.agents.morning_agent.create_react_agent",
               side_effect=fake_create):
        with patch("aistock_agent.agents.morning_agent.is_trading_day",
                   return_value=False):
            _ = [e async for e in morning_agent.stream({})]

    messages = captured.get("messages", [])
    assert messages, "messages should not be empty"
    assert "非交易日" in messages[0].content
