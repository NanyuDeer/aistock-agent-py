"""event_agent v3 单元测试 — 事件传导链分析模块化版本

验证：
- 5 个 LLM 调用编排顺序（understanding → transmission → history → investment → podcast）
- flash/deep 模型选择（understanding/history/investment/podcast = flash, transmission = deep）
- ReAct agent 工具集绑定（event 工具组 5 个工具）
- transform_to_frontend 字段映射
- str.replace 注入（非 str.format，避免 JSON 花括号崩溃）
- 缓存命中/降级/异常路径
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.agents.workers.event import (
    _analyze_history,
    _analyze_investment,
    _analyze_transmission,
    _analyze_understanding,
    _call_llm_no_tools,
    _call_llm_with_tools,
    _generate_podcast,
    run,
)

# ── Patch targets ──

_MODULE = "aistock_agent.agents.workers.event"
_GET_QUICK_THINK = f"{_MODULE}.get_quick_think"
_GET_DEEP_THINK = f"{_MODULE}.get_deep_think"
_CREATE_REACT_AGENT = f"{_MODULE}.create_react_agent"
_GET_CACHED_EVENT = f"{_MODULE}.get_cached_event"
_SET_CACHED_EVENT = f"{_MODULE}.set_cached_event"
_PERSIST_EVENT_REPORT = f"{_MODULE}.persist_event_report"

EXPECTED_TOOL_NAMES = {
    "search_cls_news",
    "get_news_fulltext",
    "get_quote",
    "tavily_finance_search",
    "match_industry_by_keywords",
}


# ── Mock data fixtures ──


def _mock_understanding() -> dict[str, object]:
    return {
        "summary": "美联储加息25个基点",
        "coreChanges": [
            {"variable": "利率", "before": "0.5%", "after": "0.75%"},
        ],
    }


def _mock_transmission() -> dict[str, object]:
    return {
        "mechanism": "加息提高资金成本",
        "variables": [
            {"name": "利率", "direction": "bearish", "strength": 0.8, "explanation": "资金成本上升"},
        ],
        "coreIndustry": {"name": "银行", "impact": "利好", "reason": "息差扩大"},
        "chain": [
            {
                "industry": "银行",
                "relation": "核心行业",
                "level": 1,
                "direction": "bullish",
                "impactStrength": 0.7,
                "reason": "息差扩大",
            },
        ],
    }


def _mock_history() -> list[object]:
    return [
        {
            "historyId": "hist_001",
            "year": "2023",
            "title": "上次加息",
            "eventType": "市场动态",
            "sentiment": "bearish",
            "industryChange": "市场下跌",
            "changePercentage": -3.5,
        },
    ]


def _mock_investment() -> dict[str, object]:
    return {
        "conclusion": "银行板块受益，中期景气改善",
        "keyPoints": ["息差扩大"],
        "focusIndustries": [{"name": "银行", "direction": "positive", "reason": "直接受益"}],
        "opportunities": ["银行股"],
        "risks": ["经济下行风险"],
        "rating": "positive",
    }


def _make_llm_mock(content: str) -> MagicMock:
    """构造 mock LLM：ainvoke 返回带 content 属性的对象。"""
    mock_llm = MagicMock()
    mock_result = MagicMock()
    mock_result.content = content
    mock_llm.ainvoke = AsyncMock(return_value=mock_result)
    return mock_llm


def _make_react_agent_mock(content: str) -> MagicMock:
    """构造 mock ReAct agent：ainvoke 返回 {"messages": [AIMessage(content)]}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={"messages": [AIMessage(content=content)]}
    )
    return mock_agent


# ── run() orchestration tests ──


@pytest.mark.asyncio
async def test_run_no_user_message() -> None:
    """空消息列表 → 返回提示文本。"""
    result = await run({"messages": []})  # type: ignore[arg-type]
    assert result == {
        "final_response": "请提供需要分析的事件描述。",
        "analysis_reports": {},
    }


@pytest.mark.asyncio
async def test_run_no_human_message() -> None:
    """消息列表无 HumanMessage → 返回提示文本。"""
    result = await run({"messages": [AIMessage(content="ai msg")]})  # type: ignore[arg-type]
    assert result["final_response"] == "请提供需要分析的事件描述。"


@pytest.mark.asyncio
async def test_run_cache_hit() -> None:
    """缓存命中 → 直接返回缓存结果，不调用 LLM。"""
    cached_data: dict[str, object] = {
        "display_report": {"event_title": "缓存事件"},
        "podcast_brief": "缓存播报文本",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]

    mock_u.assert_not_called()
    assert result["final_response"] == "缓存播报文本"
    assert result["analysis_reports"]["event_display_report"] == {"event_title": "缓存事件"}
    assert result["analysis_reports"]["event_podcast_brief"] == "缓存播报文本"


@pytest.mark.asyncio
async def test_run_understanding_failure() -> None:
    """Call 1 事件理解失败 → 返回降级文本。"""
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=None):
        with patch(
            f"{_MODULE}._analyze_understanding",
            new_callable=AsyncMock,
            return_value=None,
        ):
            result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]

    assert result == {
        "final_response": "事件分析暂时不可用，请稍后重试",
        "analysis_reports": {},
    }


@pytest.mark.asyncio
async def test_run_full_success() -> None:
    """完整流程：5 个 LLM 调用全部成功 → 返回正确的 analysis_reports 结构。"""
    understanding = _mock_understanding()
    transmission = _mock_transmission()
    history = _mock_history()
    investment = _mock_investment()
    podcast_text = "这是播报摘要文本"

    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=None):
        with patch(
            f"{_MODULE}._analyze_understanding",
            new_callable=AsyncMock,
            return_value=understanding,
        ):
            with patch(
                f"{_MODULE}._analyze_transmission",
                new_callable=AsyncMock,
                return_value=transmission,
            ):
                with patch(
                    f"{_MODULE}._analyze_history",
                    new_callable=AsyncMock,
                    return_value=history,
                ):
                    with patch(
                        f"{_MODULE}._analyze_investment",
                        new_callable=AsyncMock,
                        return_value=investment,
                    ):
                        with patch(
                            f"{_MODULE}._generate_podcast",
                            new_callable=AsyncMock,
                            return_value=podcast_text,
                        ):
                            with patch(_SET_CACHED_EVENT, new_callable=AsyncMock) as mock_set_cache:
                                with patch(
                                    _PERSIST_EVENT_REPORT,
                                    new_callable=AsyncMock,
                                ) as mock_persist:
                                    result = await run(
                                        {"messages": [HumanMessage(content="美联储加息影响")]}  # type: ignore[arg-type]
                                    )

    # final_response 是播报文本
    assert result["final_response"] == podcast_text

    # analysis_reports 包含 transform_to_frontend 的输出 + podcast_brief
    reports = result["analysis_reports"]
    assert reports["event_podcast_brief"] == podcast_text
    assert reports["event_understanding"]["summary"] == "美联储加息25个基点"
    assert reports["event_transmission"]["mechanism"] == "加息提高资金成本"
    assert len(reports["event_history"]) == 1
    assert reports["event_investment"]["conclusion"] == "银行板块受益，中期景气改善"

    # 缓存和持久化被调用
    mock_set_cache.assert_called_once()
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_run_exception_fallback() -> None:
    """run() 内部抛异常 → 返回降级文本。"""
    with patch(
        _GET_CACHED_EVENT,
        new_callable=AsyncMock,
        side_effect=RuntimeError("Redis 不可用"),
    ):
        result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]

    assert result == {
        "final_response": "事件分析暂时不可用，请稍后重试",
        "analysis_reports": {},
    }


@pytest.mark.asyncio
async def test_run_partial_failure_transmission_none() -> None:
    """Call 2 传导分析返回 None → investment 注入"无"，仍继续流程。"""
    understanding = _mock_understanding()
    investment = _mock_investment()
    podcast_text = "播报文本"

    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=None):
        with patch(
            f"{_MODULE}._analyze_understanding",
            new_callable=AsyncMock,
            return_value=understanding,
        ):
            with patch(
                f"{_MODULE}._analyze_transmission",
                new_callable=AsyncMock,
                return_value=None,
            ):
                with patch(
                    f"{_MODULE}._analyze_history",
                    new_callable=AsyncMock,
                    return_value=None,
                ):
                    with patch(
                        f"{_MODULE}._analyze_investment",
                        new_callable=AsyncMock,
                        return_value=investment,
                    ):
                        with patch(
                            f"{_MODULE}._generate_podcast",
                            new_callable=AsyncMock,
                            return_value=podcast_text,
                        ):
                            with patch(_SET_CACHED_EVENT, new_callable=AsyncMock):
                                with patch(_PERSIST_EVENT_REPORT, new_callable=AsyncMock):
                                    result = await run(
                                        {"messages": [HumanMessage(content="测试事件")]}  # type: ignore[arg-type]
                                    )

    # 仍然成功返回
    assert result["final_response"] == podcast_text
    # transmission 和 history 为 None/空
    assert result["analysis_reports"]["event_transmission"] is None
    assert result["analysis_reports"]["event_history"] == []


# ── Helper function tests (mocking LLM layer) ──


@pytest.mark.asyncio
async def test_call_llm_no_tools_success() -> None:
    """_call_llm_no_tools：LLM 返回 JSON → 解析为 dict。"""
    json_text = json.dumps({"summary": "测试", "coreChanges": []})
    mock_llm = _make_llm_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _call_llm_no_tools("system prompt", "user msg", model="flash")

    assert isinstance(result, dict)
    assert result["summary"] == "测试"


@pytest.mark.asyncio
async def test_call_llm_no_tools_failure() -> None:
    """_call_llm_no_tools：LLM 异常 → 返回 None。"""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM 不可用"))

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _call_llm_no_tools("system prompt", "user msg", model="flash")

    assert result is None


@pytest.mark.asyncio
async def test_call_llm_with_tools_success() -> None:
    """_call_llm_with_tools：ReAct agent 返回 JSON → 解析为 dict。"""
    json_text = json.dumps({"mechanism": "测试传导"})
    mock_agent = _make_react_agent_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _call_llm_with_tools("system prompt", "user msg", model="flash")

    assert isinstance(result, dict)
    assert result["mechanism"] == "测试传导"


@pytest.mark.asyncio
async def test_call_llm_with_tools_failure() -> None:
    """_call_llm_with_tools：ReAct agent 异常 → 返回 None。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("Agent 不可用"))

    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _call_llm_with_tools("system prompt", "user msg", model="flash")

    assert result is None


@pytest.mark.asyncio
async def test_call_llm_with_tools_binds_event_tools() -> None:
    """_call_llm_with_tools 使用 get_tools("event") 获取工具集。"""
    json_text = json.dumps({"mechanism": "测试"})
    mock_agent = _make_react_agent_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent) as mock_create:
            await _call_llm_with_tools("system", "user", model="flash")

    mock_create.assert_called_once()
    tools_arg = mock_create.call_args[0][1]
    assert {t.name for t in tools_arg} == EXPECTED_TOOL_NAMES


@pytest.mark.asyncio
async def test_analyze_understanding_uses_flash() -> None:
    """_analyze_understanding 使用 get_quick_think（flash 模型）。"""
    json_text = json.dumps({"summary": "测试", "coreChanges": []})
    mock_llm = _make_llm_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=mock_llm) as mock_quick:
        with patch(_GET_DEEP_THINK, return_value=MagicMock()) as mock_deep:
            await _analyze_understanding("测试事件")

    mock_quick.assert_called_once()
    mock_deep.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_transmission_uses_deep() -> None:
    """_analyze_transmission 使用 get_deep_think（deep 模型）。"""
    json_text = json.dumps({"mechanism": "测试"})
    mock_agent = _make_react_agent_mock(json_text)

    with patch(_GET_DEEP_THINK, return_value=MagicMock()) as mock_deep:
        with patch(_GET_QUICK_THINK, return_value=MagicMock()) as mock_quick:
            with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
                await _analyze_transmission("测试事件", {"summary": "测试"})

    mock_deep.assert_called_once()
    mock_quick.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_history_returns_list() -> None:
    """_analyze_history 返回 list（LLM 输出 JSON 数组）。"""
    json_text = json.dumps([{"historyId": "h001", "year": "2023", "title": "测试"}])
    mock_agent = _make_react_agent_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _analyze_history("测试事件", {"summary": "测试"})

    assert isinstance(result, list)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_analyze_history_dict_wrapped_to_list() -> None:
    """_analyze_history：LLM 输出单对象 → 包装为 list。"""
    json_text = json.dumps({"historyId": "h001", "year": "2023", "title": "测试"})
    mock_agent = _make_react_agent_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _analyze_history("测试事件", {"summary": "测试"})

    assert isinstance(result, list)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_analyze_investment_uses_replace_not_format() -> None:
    """_analyze_investment 使用 str.replace 注入上下文（非 str.format）。

    验证 prompt 中的 JSON 花括号不会导致 format 崩溃。
    如果使用了 str.format()，此测试会因 KeyError/IndexError 崩溃。
    """
    json_text = json.dumps({"conclusion": "测试结论", "rating": "positive"})
    mock_llm = _make_llm_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _analyze_investment(
            {"summary": "测试理解"},
            {"mechanism": "测试传导"},
            [{"historyId": "h001"}],
        )

    assert isinstance(result, dict)
    assert result["conclusion"] == "测试结论"


@pytest.mark.asyncio
async def test_analyze_investment_none_inputs() -> None:
    """_analyze_investment：输入全为 None → 不崩溃，注入"无"。"""
    json_text = json.dumps({"conclusion": "无足够数据", "rating": "neutral"})
    mock_llm = _make_llm_mock(json_text)

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _analyze_investment(None, None, None)

    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_generate_podcast_returns_text() -> None:
    """_generate_podcast 返回纯文本。"""
    mock_llm = _make_llm_mock("这是150字的播报摘要文本。")

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _generate_podcast({"summary": "测试"}, "测试结论")

    assert isinstance(result, str)
    assert "播报摘要" in result


@pytest.mark.asyncio
async def test_generate_podcast_failure_returns_fallback() -> None:
    """_generate_podcast：LLM 异常 → 返回错误提示。"""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM 不可用"))

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _generate_podcast({"summary": "测试"}, "测试结论")

    assert result == "事件播报生成失败，请稍后重试"


@pytest.mark.asyncio
async def test_generate_podcast_uses_replace_not_format() -> None:
    """_generate_podcast 使用 str.replace 注入（非 str.format）。

    验证 prompt 中的 JSON 花括号不会导致 format 崩溃。
    如果使用了 str.format()，此测试会因 KeyError/IndexError 崩溃。
    """
    mock_llm = _make_llm_mock("播报文本")

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _generate_podcast({"summary": "测试摘要"}, "测试结论")

    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_generate_podcast_none_understanding() -> None:
    """_generate_podcast：understanding 为 None → summary 为空字符串。"""
    mock_llm = _make_llm_mock("播报文本")

    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _generate_podcast(None, "测试结论")

    assert isinstance(result, str)
