"""morning_agent 测试"""
from datetime import date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.agents.workers import morning as morning_agent
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


# ── run() 测试 ────────────────────────────────────────────────────
# 函数迁至 services/cache.py / services/archiver.py 后的 patch 路径
_MORNING_GET_CACHED = "aistock_agent.agents.workers.morning.get_cached_briefing"
_MORNING_SET_CACHED = "aistock_agent.agents.workers.morning.set_cached_briefing"
_MORNING_ARCHIVE = "aistock_agent.agents.workers.morning.archive_morning"
_MORNING_CREATE_AGENT = "aistock_agent.agents.workers.morning.create_react_agent"
_MORNING_GET_DEEP = "aistock_agent.agents.workers.morning.get_deep_think"

# run() 期望绑定的工具集（集合断言，不依赖顺序）
_MORNING_EXPECTED_TOOL_NAMES = {"tavily_finance_search", "get_global_markets", "get_cls_news"}


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

    assert result["final_response"] == "cached content"
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_morning_run_cache_miss_invokes_agent():
    """缓存未命中：调用 create_react_agent，tools 列表正确。"""
    mock_agent = _make_mock_morning_agent([AIMessage(content="晨报内容")])
    with patch(_MORNING_GET_CACHED, AsyncMock(return_value=None)):
        with patch(_MORNING_GET_DEEP, return_value=MagicMock()):
            with patch(_MORNING_CREATE_AGENT, return_value=mock_agent) as mock_create:
                with patch(_MORNING_SET_CACHED, AsyncMock()):
                    with patch(_MORNING_ARCHIVE):
                        result = await morning_agent.run({})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert {t.name for t in tools_arg} == _MORNING_EXPECTED_TOOL_NAMES


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
                    with patch(_MORNING_ARCHIVE):
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
                    with patch(_MORNING_ARCHIVE):
                        result = await morning_agent.run({})

    assert result["final_response"] == "最终晨报内容"
    mock_set.assert_awaited_once_with("最终晨报内容")


@pytest.mark.asyncio
async def test_morning_run_non_trading_day_injects_prompt():
    """非交易日时 system_prompt 包含非交易日提示。"""
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
                    with patch(_MORNING_ARCHIVE):
                        with patch("aistock_agent.agents.workers.morning.is_trading_day", return_value=False):
                            await morning_agent.run({})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert "非交易日" in messages[0].content
