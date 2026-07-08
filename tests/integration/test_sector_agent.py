"""sector_agent run() 单元测试 — 板块分析

mock create_react_agent，验证：
- 工具集绑定（get_leader_stocks, get_capital_flow）
- SystemMessage 注入（SECTOR_ANALYST_PROMPT）
- final_response 提取
- 使用 get_deep_think（非 quick_think）— sector 的入口校验项
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.agents.workers.sector import run
from aistock_agent.prompts.workers.sector import SECTOR_ANALYST_PROMPT

_CREATE_REACT_AGENT = "aistock_agent.agents.workers.sector.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.sector.get_deep_think"

EXPECTED_TOOL_NAMES = {"get_leader_stocks", "get_capital_flow"}


def _make_mock_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


@pytest.mark.asyncio
async def test_sector_agent_tools_bound_correctly():
    """create_react_agent 被调用时 tools 参数为正确的 2 个工具。"""
    mock_agent = _make_mock_agent([AIMessage(content="板块分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"messages": [HumanMessage(content="分析白酒板块")]})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert {t.name for t in tools_arg} == EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_sector_agent_system_message_injected():
    """ainvoke 传入的 messages 首条为 SystemMessage，内容为 SECTOR_ANALYST_PROMPT。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [HumanMessage(content="分析白酒板块")]})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == SECTOR_ANALYST_PROMPT


@pytest.mark.asyncio
async def test_sector_agent_extracts_final_ai_response():
    """从多条消息中提取最后一条 AI 回复作为 final_response。"""
    messages = [
        HumanMessage(content="分析白酒板块"),
        AIMessage(content="中间过程"),
        AIMessage(content="板块最终结论"),
    ]
    mock_agent = _make_mock_agent(messages)
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"messages": [HumanMessage(content="分析白酒板块")]})

    assert result == {"final_response": "板块最终结论"}


@pytest.mark.asyncio
async def test_sector_agent_uses_deep_think_llm():
    """sector agent 使用 get_deep_think（非 quick_think）。"""
    mock_agent = _make_mock_agent([AIMessage(content="done")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()) as mock_deep:
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [HumanMessage(content="分析白酒板块")]})

    mock_deep.assert_called_once()
