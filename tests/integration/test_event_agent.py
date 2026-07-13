"""event_agent run() 单元测试 — 事件传导链分析

mock create_react_agent，验证：
- 工具集绑定（search_cls_news, get_news_fulltext, get_quote, tavily_finance_search）
- SystemMessage 注入（EVENT_ANALYST_PROMPT）
- final_response 提取
- 使用 get_deep_think（非 quick_think）— event 的入口校验项
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.agents.workers.event import run
from aistock_agent.prompts.workers.event import EVENT_ANALYST_PROMPT

_CREATE_REACT_AGENT = "aistock_agent.agents.workers.event.create_react_agent"
_GET_DEEP_THINK = "aistock_agent.agents.workers.event.get_deep_think"

EXPECTED_TOOL_NAMES = {
    "search_cls_news", "get_news_fulltext", "get_quote", "tavily_finance_search",
    "match_industry_by_keywords",
}


def _make_mock_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


@pytest.mark.asyncio
async def test_event_agent_tools_bound_correctly():
    """create_react_agent 被调用时 tools 参数为正确的 4 个工具。"""
    mock_agent = _make_mock_agent([AIMessage(content="事件分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"messages": [HumanMessage(content="美联储加息影响")]})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert {t.name for t in tools_arg} == EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_event_agent_system_message_injected():
    """ainvoke 传入的 messages 首条为 SystemMessage，内容为 EVENT_ANALYST_PROMPT。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent.ainvoke = fake_ainvoke

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [HumanMessage(content="美联储加息影响")]})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == EVENT_ANALYST_PROMPT


@pytest.mark.asyncio
async def test_event_agent_extracts_final_ai_response():
    """从多条消息中提取最后一条 AI 回复作为 final_response。"""
    messages = [
        HumanMessage(content="美联储加息影响"),
        AIMessage(content="中间过程"),
        AIMessage(content="事件传导结论"),
    ]
    mock_agent = _make_mock_agent(messages)
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"messages": [HumanMessage(content="美联储加息影响")]})

    assert result == {"final_response": "事件传导结论", "analysis_reports": {}}


@pytest.mark.asyncio
async def test_event_agent_uses_deep_think_llm():
    """event agent 使用 get_deep_think（非 quick_think）。"""
    mock_agent = _make_mock_agent([AIMessage(content="done")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()) as mock_deep:
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [HumanMessage(content="美联储加息影响")]})

    mock_deep.assert_called_once()


@pytest.mark.asyncio
async def test_event_agent_podcast_brief_degradation():
    """LLM 输出不含 podcast_brief 时，回退到 extract_final_ai_response 原始文本。

    模拟 LLM 返回的 JSON 只有 display_report 没有 podcast_brief，
    验证 run() 不会崩溃，而是走降级路径返回 final_response。
    """
    import json

    # 只有 display_report，没有 podcast_brief → parse_event_output 返回 (display, None)
    degraded_output = json.dumps({
        "display_report": {"event_title": "测试事件", "impact_level": 3},
    })
    messages = [AIMessage(content=degraded_output)]
    mock_agent = _make_mock_agent(messages)

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await run({"messages": [HumanMessage(content="测试事件")]})

    # podcast_brief 为 None → 走 fallback 路径，final_response 为 extract_final_ai_response 的原始文本
    assert "final_response" in result
    assert isinstance(result["final_response"], str)
    # 降级时不应返回 podcast_brief 缓存的结果
    assert result.get("analysis_reports") == {}


@pytest.mark.asyncio
async def test_event_agent_includes_match_industry_tool():
    """create_react_agent 的工具集包含 match_industry_by_keywords（pgvector 语义匹配）"""
    mock_agent = _make_mock_agent([AIMessage(content="事件分析完成")])
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await run({"messages": [HumanMessage(content="新能源汽车补贴退坡影响")]})

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    tool_names = {t.name for t in tools_arg}
    assert "match_industry_by_keywords" in tool_names, (
        f"工具集应包含 match_industry_by_keywords，实际: {tool_names}"
    )


@pytest.mark.asyncio
async def test_event_agent_user_context_passed_through():
    """用户消息内容被正确传入 agent.ainvoke 的 messages 参数中。

    验证最后一条 HumanMessage 的内容出现在传给 LLM 的消息里。
    """
    captured: dict = {}

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="分析完成")]}

    mock_agent = MagicMock()
    mock_agent.ainvoke = fake_ainvoke

    user_text = "美国对中国半导体加征关税，分析产业链影响"

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            await run({"messages": [
                HumanMessage(content="旧消息"),
                HumanMessage(content=user_text),
            ]})

    messages = captured["messages"]
    # SystemMessage + 最近 5 条历史消息（其中含 user_text）
    human_msgs = [m for m in messages if isinstance(m, HumanMessage)]
    assert len(human_msgs) >= 1, "应至少包含一条 HumanMessage"

    # user_text 应出现在某条 HumanMessage 的 content 中
    human_contents = [str(m.content) for m in human_msgs]
    assert any("加征关税" in c for c in human_contents), (
        f"用户消息应包含'加征关税'，实际: {human_contents}"
    )


@pytest.mark.asyncio
async def test_event_agent_exception_returns_fallback():
    """LLM 调用完全失败时，返回降级文本。"""
    with patch(_GET_DEEP_THINK, side_effect=RuntimeError("LLM 不可用")):
        result = await run({"messages": [HumanMessage(content="测试事件")]})

    assert result == {
        "final_response": "事件分析暂时不可用，请稍后重试",
        "analysis_reports": {},
    }
