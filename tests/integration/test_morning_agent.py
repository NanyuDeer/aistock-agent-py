"""morning_agent 测试"""
from datetime import date

import pytest

from aistock_agent.agents.workers.morning import is_trading_day


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

from aistock_agent.agents.workers import morning as morning_agent


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

    with patch("aistock_agent.agents.workers.morning.create_react_agent",
               return_value=mock_agent):
        with patch("aistock_agent.agents.workers.morning.is_trading_day",
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

    with patch("aistock_agent.agents.workers.morning.create_react_agent",
               return_value=mock_agent):
        with patch("aistock_agent.agents.workers.morning.is_trading_day",
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

    with patch("aistock_agent.agents.workers.morning.create_react_agent",
               side_effect=fake_create):
        with patch("aistock_agent.agents.workers.morning.is_trading_day",
                   return_value=False):
            _ = [e async for e in morning_agent.stream({})]

    messages = captured.get("messages", [])
    assert messages, "messages should not be empty"
    assert "非交易日" in messages[0].content


# ── run() 测试 ────────────────────────────────────────────────────
from datetime import datetime

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.tools.market_tools import get_global_markets, tavily_finance_search
from aistock_agent.tools.news_tools import get_cls_news

_MORNING_GET_CACHED = "aistock_agent.agents.workers.morning._get_cached_briefing"
_MORNING_SET_CACHED = "aistock_agent.agents.workers.morning._set_cached_briefing"
_MORNING_CREATE_AGENT = "aistock_agent.agents.workers.morning.create_react_agent"
_MORNING_GET_DEEP = "aistock_agent.agents.workers.morning.get_deep_think"

# run() 期望绑定的工具集（与 morning.py run 中 tools 列表顺序一致）
_MORNING_EXPECTED_TOOLS = [tavily_finance_search, get_global_markets, get_cls_news]


def _make_mock_morning_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


@pytest.mark.asyncio
async def test_morning_run_cache_hit_returns_cached():
    """缓存命中：直接返回缓存内容，不调用 create_react_agent。"""
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value="cached content")):
        with patch(_MORNING_CREATE_AGENT) as mock_create:
            result = await morning_agent.run({})

    assert result == {"final_response": "cached content"}
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_morning_run_cache_miss_invokes_agent():
    """缓存未命中：调用 create_react_agent，tools 列表正确。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content="晨报内容")])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent) as mock_create:
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    result = await morning_agent.run({})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert tools_arg == _MORNING_EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_morning_run_system_message_injected():
    """ainvoke 传入的 messages 首条为 SystemMessage，content 含今日日期。"""
    today = datetime.now().strftime("%Y年%m月%d日")
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="晨报")]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    await morning_agent.run({})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert today in messages[0].content


@pytest.mark.asyncio
async def test_morning_run_extracts_and_caches_response():
    """从 messages 提取最后一条 AI 回复作为 final_response，并写入缓存。"""
    messages = [
        HumanMessage(content="生成晨报"),
        AIMessage(content="中间过程"),
        AIMessage(content="最终晨报内容"),
    ]
    mock_agent = _make_mock_morning_agent(messages)
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent):
                with patch(_MORNING_SET_CACHED, AsyncMock()) as mock_set:
                    result = await morning_agent.run({})

    assert result == {"final_response": "最终晨报内容"}
    mock_set.assert_awaited_once_with("最终晨报内容")
