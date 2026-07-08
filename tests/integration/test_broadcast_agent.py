"""播报 Agent 集成测试

覆盖：提示词占位符替换 + 响应提取 + 异常降级
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage

from aistock_agent.agents.workers.broadcast import run

_GET_DEEP_THINK = "aistock_agent.agents.workers.broadcast.get_deep_think"


def _make_mock_llm(response_content: str) -> MagicMock:
    """构造 mock LLM，ainvoke 返回单个 AIMessage"""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=response_content))
    return mock_llm


@pytest.mark.asyncio
async def test_broadcast_prompt_placeholders_replaced():
    """验证提示词占位符被正确替换"""
    mock_llm = _make_mock_llm("播报内容")
    with patch(_GET_DEEP_THINK, return_value=mock_llm):
        await run({
            "messages": [],
            "analysis_reports": {"morning": "晨报内容", "wind_leader": "风口内容"},
        })

    invoke_args = mock_llm.ainvoke.call_args[0][0]
    system_msg = invoke_args[0]
    assert "晨报内容" in system_msg.content
    assert "风口内容" in system_msg.content


@pytest.mark.asyncio
async def test_broadcast_default_placeholders():
    """验证无分析结果时占位符使用默认值"""
    mock_llm = _make_mock_llm("播报内容")
    with patch(_GET_DEEP_THINK, return_value=mock_llm):
        await run({"messages": [], "analysis_reports": {}})

    invoke_args = mock_llm.ainvoke.call_args[0][0]
    system_msg = invoke_args[0]
    assert "暂无晨报" in system_msg.content
    assert "暂无长线风口分析" in system_msg.content


@pytest.mark.asyncio
async def test_broadcast_response_extraction():
    """验证 final_response 正确提取"""
    expected = "主持人：大家好...分析师：今日风口..."
    mock_llm = _make_mock_llm(expected)
    with patch(_GET_DEEP_THINK, return_value=mock_llm):
        result = await run({
            "messages": [],
            "analysis_reports": {"morning": "晨报", "wind_leader": "风口"},
        })

    assert result["final_response"] == expected


@pytest.mark.asyncio
async def test_broadcast_exception_degradation():
    """验证 LLM 异常时返回降级文本"""
    with patch(_GET_DEEP_THINK, side_effect=Exception("LLM error")):
        result = await run({"messages": [], "analysis_reports": {}})

    assert result["final_response"] == "播报生成暂时不可用，请稍后重试"
