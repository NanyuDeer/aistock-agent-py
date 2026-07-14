"""ai_advisor agent 单元测试"""

import pytest

from aistock_agent.state.schema import AgentState


def test_ai_advisor_prompt_exists():
    """AI_ADVISOR_PROMPT 常量存在且包含必要占位符"""
    from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT

    assert isinstance(AI_ADVISOR_PROMPT, str)
    assert len(AI_ADVISOR_PROMPT) > 50
    assert "{{AVAILABLE_REPORTS}}" in AI_ADVISOR_PROMPT


def test_ai_advisor_prompt_replaceable():
    """AI_ADVISOR_PROMPT 中的 {{AVAILABLE_REPORTS}} 可被替换"""
    from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT

    result = AI_ADVISOR_PROMPT.replace("{{AVAILABLE_REPORTS}}", "测试报告内容")
    assert "{{AVAILABLE_REPORTS}}" not in result
    assert "测试报告内容" in result


# ── 任务2: ai_advisor agent 节点测试 ──────────────────────────────────


def _make_state(
    intent: str = "morning",
    messages: list | None = None,
    trigger_source: str | None = "user",
    report_date: str = "2026-07-10",
    symbol: str | None = None,
) -> AgentState:
    from langchain_core.messages import HumanMessage
    return {
        "messages": messages or [HumanMessage(content="今天市场怎么样")],
        "session_id": "test-session",
        "user_id": None,
        "favorites": [],
        "intent": intent,
        "symbol": symbol,
        "tag_code": None,
        "analysis_reports": {},
        "trigger_source": trigger_source,
        "report_date": report_date,
        "final_response": None,
    }


@pytest.mark.asyncio
async def test_run_with_reports(monkeypatch):
    """有报告时直接用 LLM 汇总"""
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        if report_type == "morning":
            return {"content": {"text": "今日市场高开低走，成交量放大"}}
        return None

    class MockLLM:
        async def ainvoke(self, messages, **kwargs):
            class Resp:
                content = "根据晨报，今日市场高开低走"
            return Resp()

    import aistock_agent.agents.workers.ai_advisor as mod
    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: MockLLM())

    state = _make_state(intent="morning")
    result = await run(state)
    assert result["final_response"] is not None
    assert "高开低走" in result["final_response"]


@pytest.mark.asyncio
async def test_run_no_reports_fallback_to_react(monkeypatch):
    """无报告时降级使用 ReAct Agent"""
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return None

    class MockAgent:
        async def ainvoke(self, input_dict, **kwargs):
            from langchain_core.messages import AIMessage
            return {"messages": [AIMessage(content="根据工具数据，市场整体偏弱")]}

    import aistock_agent.agents.workers.ai_advisor as mod
    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "create_react_agent", lambda llm, tools: MockAgent())
    monkeypatch.setattr(mod, "get_tools", lambda cat: [])

    state = _make_state(intent="morning")
    result = await run(state)
    assert result["final_response"] is not None
    assert "偏弱" in result["final_response"]


@pytest.mark.asyncio
async def test_run_exception_returns_fallback(monkeypatch):
    """异常时返回降级文本"""
    from aistock_agent.agents.workers.ai_advisor import run

    async def mock_get_report(report_type: str, report_date: str, **kwargs):
        return None

    def _raise(exc):
        raise exc

    import aistock_agent.agents.workers.ai_advisor as mod
    monkeypatch.setattr(mod.node_api, "get_analysis_report", mock_get_report)
    monkeypatch.setattr(mod, "get_deep_think", lambda: _raise(Exception("LLM down")))

    state = _make_state(intent="morning")
    result = await run(state)
    assert "暂时不可用" in result["final_response"]
