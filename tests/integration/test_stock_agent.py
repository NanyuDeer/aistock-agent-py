"""stock_agent run() 单元测试 — 个股综合分析

mock create_react_agent（不依赖真实 LLM/网络），验证：
- 工具集绑定正确（get_quote, get_capital_flow, get_profit_forecast, search_cls_news）
- SystemMessage 注入（内容为 STOCK_ANALYST_PROMPT）
- final_response 提取（取最后一条 AI 回复）
- symbol 缺失时返回提示文本（入口校验）
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.agents.workers.stock import run
from aistock_agent.prompts.workers.stock import STOCK_ANALYST_PROMPT
from aistock_agent.tools.news_tools import search_cls_news
from aistock_agent.tools.stock_tools import get_capital_flow, get_profit_forecast, get_quote

_CREATE_REACT_AGENT = "aistock_agent.agents.workers.stock.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.stock.get_deep_think"

# 期望绑定的工具集（与 stock.py 中 tools 列表顺序一致）
EXPECTED_TOOLS = [get_quote, get_capital_flow, get_profit_forecast, search_cls_news]


def _make_mock_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


@pytest.mark.asyncio
async def test_stock_agent_tools_bound_correctly():
    """create_react_agent 被调用时 tools 参数为正确的 4 个工具。"""
    mock_agent = _make_mock_agent([AIMessage(content="分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]})

    mock_create.assert_called_once()
    # create_react_agent(llm, tools) 位置参数：[0]=llm, [1]=tools
    tools_arg = mock_create.call_args[0][1]
    assert tools_arg == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_stock_agent_system_message_injected():
    """ainvoke 传入的 messages 首条为 SystemMessage，内容为 STOCK_ANALYST_PROMPT。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == STOCK_ANALYST_PROMPT


@pytest.mark.asyncio
async def test_stock_agent_extracts_final_ai_response():
    """从多条消息中提取最后一条 AI 回复作为 final_response。"""
    messages = [
        HumanMessage(content="分析 600519"),
        AIMessage(content="中间思考过程"),
        AIMessage(content="最终回复：贵州茅台"),
    ]
    mock_agent = _make_mock_agent(messages)
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]})

    assert result == {"final_response": "最终回复：贵州茅台"}


@pytest.mark.asyncio
async def test_stock_agent_symbol_missing_returns_hint():
    """symbol 缺失时返回提示文本，不调用 LLM 与 create_react_agent。"""
    mock_llm = MagicMock()
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock()
    with patch(_GET_DEEP_THINK, return_value=mock_llm) as mock_llm_factory:
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            result = await run({"messages": [HumanMessage(content="分析一下")]})

    assert result == {"final_response": "请提供股票代码，例如：分析一下 600519"}
    # 缺 symbol 应在入口提前返回，不触达 LLM 与 agent 创建
    mock_llm_factory.assert_not_called()
    mock_create.assert_not_called()
