"""alert_agent run() 集成测试 — 异动提醒分析

mock create_react_agent，验证：
- 工具集绑定（get_stock_monitor, get_alert_history, get_quote, get_capital_flow, search_cls_news）
- SystemMessage 注入（ALERT_ANALYST_PROMPT）
- final_response 提取
- symbol 缺失时返回提示文本（入口校验）
- 使用 get_deep_think（非 quick_think）
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.agents.workers.alert import run
from aistock_agent.prompts.workers.alert import ALERT_ANALYST_PROMPT

_CREATE_REACT_AGENT = "aistock_agent.agents.workers.alert.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.alert.get_deep_think"

EXPECTED_TOOL_NAMES = {
    "get_stock_monitor", "get_alert_history",
    "get_quote", "get_capital_flow", "search_cls_news",
}


def _make_mock_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


@pytest.mark.asyncio
async def test_alert_agent_tools_bound_correctly():
    """create_react_agent 被调用时 tools 参数为正确的 5 个工具。"""
    mock_agent = _make_mock_agent([AIMessage(content="异动分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519 异动")]})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert {t.name for t in tools_arg} == EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_alert_agent_system_message_injected():
    """ainvoke 传入的 messages 首条为 SystemMessage，内容为 ALERT_ANALYST_PROMPT。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519 异动")]})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == ALERT_ANALYST_PROMPT


@pytest.mark.asyncio
async def test_alert_agent_extracts_final_ai_response():
    """从多条消息中提取最后一条 AI 回复作为 final_response。"""
    messages = [
        HumanMessage(content="分析 600519 异动"),
        AIMessage(content="中间分析过程"),
        AIMessage(content=" 发生了什么：贵州茅台近期异动事件如下..."),
    ]
    mock_agent = _make_mock_agent(messages)
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519 异动")]})

    assert result == {"final_response": " 发生了什么：贵州茅台近期异动事件如下..."}


@pytest.mark.asyncio
async def test_alert_agent_symbol_missing_returns_hint():
    """symbol 缺失时返回提示文本，不调用 LLM。"""
    mock_llm = MagicMock()
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock()
    with patch(_GET_DEEP_THINK, return_value=mock_llm) as mock_llm_factory:
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            result = await run({"messages": [HumanMessage(content="有什么异动")]})

    assert result == {"final_response": "请提供股票代码，例如：分析一下 600519 的异动"}
    mock_llm_factory.assert_not_called()
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_alert_agent_uses_deep_think_llm():
    """alert agent 使用 get_deep_think（非 quick_think）。"""
    mock_agent = _make_mock_agent([AIMessage(content="done")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()) as mock_deep:
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519 异动")]})

    mock_deep.assert_called_once()
