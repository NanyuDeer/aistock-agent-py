"""general_agent run() 单元测试 — 兜底节点

mock create_react_agent，验证：
- 工具集绑定（get_quote）
- SystemMessage 注入（GENERAL_PROMPT）
- final_response 提取
- 使用 get_quick_think（非 deep_think）— general 的关键差异，入口校验项
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.agents.general.node import run
from aistock_agent.prompts.general.system import GENERAL_PROMPT
from aistock_agent.tools.stock_tools import get_quote

_CREATE_REACT_AGENT = "aistock_agent.agents.general.node.create_react_agent"
_GET_QUICK_THINK = "aistock_agent.agents.general.node.get_quick_think"

EXPECTED_TOOLS = [get_quote]


def _make_mock_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


@pytest.mark.asyncio
async def test_general_agent_tools_bound_correctly():
    """create_react_agent 被调用时 tools 参数为 get_quote。"""
    mock_agent = _make_mock_agent([AIMessage(content="兜底回复")])
    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"messages": [HumanMessage(content="你好")]})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert tools_arg == EXPECTED_TOOLS


@pytest.mark.asyncio
async def test_general_agent_system_message_injected():
    """ainvoke 传入的 messages 首条为 SystemMessage，内容为 GENERAL_PROMPT。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [HumanMessage(content="你好")]})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == GENERAL_PROMPT


@pytest.mark.asyncio
async def test_general_agent_extracts_final_ai_response():
    """从多条消息中提取最后一条 AI 回复作为 final_response。"""
    messages = [
        HumanMessage(content="你好"),
        AIMessage(content="中间过程"),
        AIMessage(content="最终兜底回复"),
    ]
    mock_agent = _make_mock_agent(messages)
    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"messages": [HumanMessage(content="你好")]})

    assert result == {"final_response": "最终兜底回复"}


@pytest.mark.asyncio
async def test_general_agent_uses_quick_think_llm():
    """general agent 使用 get_quick_think（非 deep_think，这是 general 的关键差异）。"""
    mock_agent = _make_mock_agent([AIMessage(content="done")])
    with patch(_GET_QUICK_THINK, return_value=MagicMock()) as mock_quick:
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [HumanMessage(content="你好")]})

    mock_quick.assert_called_once()
