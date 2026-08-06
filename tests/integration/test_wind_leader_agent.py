"""长线风口 Agent 集成测试

覆盖：工具集绑定 + 提示词注入 + 响应提取 + 异常降级
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.agents.workers.wind_leader import run
from aistock_agent.prompts.workers.wind_leader import WIND_LEADER_ANALYST_PROMPT
from aistock_agent.tools.sector_tools import get_wind_leaders

_CREATE_REACT_AGENT = "aistock_agent.agents.workers.wind_leader.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.wind_leader.get_deep_think"


def _make_mock_agent(responses: list[AIMessage]) -> MagicMock:
    """构造 mock agent，ainvoke 返回包含指定 AI 响应的结果"""
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value={"messages": responses})
    return mock


@pytest.mark.asyncio
async def test_wind_leader_tools_bound_correctly():
    """验证 get_wind_leaders 工具绑定到 create_react_agent"""
    mock_agent = _make_mock_agent([AIMessage(content="风口分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"messages": [HumanMessage(content="看看长线风口")], "analysis_reports": {}})

    tools_arg = mock_create.call_args[0][1]
    assert set(t.name for t in tools_arg) == {"get_wind_leaders"}


@pytest.mark.asyncio
async def test_wind_leader_prompt_injected():
    """验证 SystemMessage 注入的是 WIND_LEADER_ANALYST_PROMPT"""
    mock_agent = _make_mock_agent([AIMessage(content="风口分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [], "analysis_reports": {}})

    invoke_args = mock_agent.ainvoke.call_args[0][0]
    messages = invoke_args["messages"]
    assert messages[0].content == WIND_LEADER_ANALYST_PROMPT


@pytest.mark.asyncio
async def test_wind_leader_response_extraction():
    """验证 final_response 正确提取"""
    expected = "今日风口：人工智能板块上榜5次..."
    mock_agent = _make_mock_agent([AIMessage(content=expected)])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"messages": [], "analysis_reports": {}})

    assert result["final_response"] == expected


@pytest.mark.asyncio
async def test_wind_leader_analysis_reports_written():
    """验证 analysis_reports 写入 wind_leader 键"""
    expected = "风口分析结果"
    mock_agent = _make_mock_agent([AIMessage(content=expected)])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"messages": [], "analysis_reports": {"morning": "晨报"}})

    assert result["analysis_reports"]["wind_leader"] == expected
    assert result["analysis_reports"]["morning"] == "晨报"


@pytest.mark.asyncio
async def test_wind_leader_scheduler_persists_real_source():
    """scheduler 产出的风口工件必须记录真实生产者来源。"""
    expected = "风口分析结果"
    mock_agent = _make_mock_agent([AIMessage(content=expected)])
    with (
        patch(_GET_DEEP_THINK, return_value=MagicMock()),
        patch(_CREATE_REACT_AGENT, return_value=mock_agent),
        patch(
            "aistock_agent.agents.workers.wind_leader.ensure_data_available",
            new_callable=AsyncMock,
            return_value=True,
        ),
        # mock 响应非双层 JSON，会触发 repair_dual_layer_with_llm 的真实 LLM 调用；
        # 这里固定返回 None 走降级分支，避免依赖 OPENAI_API_KEY 等外部环境
        patch(
            "aistock_agent.agents.workers.wind_leader.repair_dual_layer_with_llm",
            new_callable=AsyncMock,
            return_value=None,
        ),
        patch("aistock_agent.agents.workers.wind_leader._archive_wind_leader"),
        patch("aistock_agent.agents.workers.wind_leader.node_api") as mock_api,
    ):
        mock_api.save_analysis_report = AsyncMock(return_value={"id": 1})
        await run({
            "messages": [],
            "analysis_reports": {},
            "trigger_source": "scheduler",
            "report_date": "2026-07-24",
        })

    assert mock_api.save_analysis_report.await_args.kwargs["data_source"] == "wind_leader_agent"


@pytest.mark.asyncio
async def test_wind_leader_exception_degradation():
    """验证 LLM 异常时返回降级文本"""
    with patch(_GET_DEEP_THINK, side_effect=Exception("LLM error")):
        result = await run({"messages": [], "analysis_reports": {}})

    assert result["final_response"] == "风口龙头分析暂时不可用，请稍后重试"
