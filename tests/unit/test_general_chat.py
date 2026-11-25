"""general chat 双模式入口测试（P7+P8 线 1 Task 2）。"""
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.general.chat import run_gap, run_science


@pytest.mark.asyncio
async def test_run_science_returns_llm_text() -> None:
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content="市盈率是股价与每股收益的比值")
    with patch("aistock_agent.agents.general.chat.get_quick_think", return_value=llm):
        reply = await run_science("什么是市盈率")
    assert "市盈率" in reply


@pytest.mark.asyncio
async def test_run_science_degrades_on_exception() -> None:
    with patch(
        "aistock_agent.agents.general.chat.get_quick_think",
        side_effect=RuntimeError("llm down"),
    ):
        reply = await run_science("什么是市盈率")
    assert "暂不可用" in reply  # 不抛异常


@pytest.mark.asyncio
async def test_run_gap_uses_tavily_tool() -> None:
    agent = AsyncMock()
    agent.ainvoke.return_value = {
        "messages": [AsyncMock(content="美联储加息的影响需结合汇率与资本流动分析")]
    }
    with patch(
        "aistock_agent.agents.general.chat.create_react_agent", return_value=agent
    ) as create_mock, patch(
        "aistock_agent.agents.general.chat.get_quick_think"
    ) as _llm_mock:
        reply = await run_gap("美联储加息对A股有什么影响")
    # 确认 ReAct agent 用 tavily_finance_search 工具构建
    _, kwargs = create_mock.call_args
    tool_names = [getattr(t, "name", "") for t in kwargs["tools"]]
    assert "tavily_finance_search" in tool_names
    assert "加息" in reply


@pytest.mark.asyncio
async def test_run_gap_degrades_on_exception() -> None:
    with patch(
        "aistock_agent.agents.general.chat.create_react_agent",
        side_effect=RuntimeError("graph down"),
    ):
        reply = await run_gap("美联储加息对A股有什么影响")
    assert "暂不可用" in reply  # 不抛异常
