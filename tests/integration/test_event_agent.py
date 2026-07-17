"""event_agent v3 单元测试 — 事件传导链分析模块化版本

验证：
- 5 个 LLM 调用编排顺序
- flash/deep 模型选择
- transform_to_frontend 字段映射
- 缓存命中/降级/异常路径
- title 元数据清洁（P1：不得含指令前缀/不回退到 user_msg）
- podcast_brief [150,200] 总函数校验（P1）
"""

import json
from contextlib import ExitStack
from typing import Any
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
    _truncate_at_sentence_boundary,
    _validate_podcast_brief,
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


def _mock_understanding(summary: str = "美联储加息25个基点") -> dict[str, object]:
    return {
        "summary": summary,
        "coreChanges": [{"variable": "利率", "before": "0.5%", "after": "0.75%"}],
    }


def _mock_transmission() -> dict[str, object]:
    return {
        "mechanism": "加息提高资金成本",
        "variables": [{
            "name": "利率", "direction": "bearish", "strength": 0.8,
            "explanation": "资金成本上升",
        }],
        "coreIndustry": {"name": "银行", "impact": "利好", "reason": "息差扩大"},
        "chain": [{
            "industry": "银行", "relation": "核心行业", "level": 1,
            "direction": "bullish", "impactStrength": 0.7, "reason": "息差扩大",
        }],
    }


def _mock_history() -> list[object]:
    return [{
        "historyId": "hist_001", "year": "2023", "title": "上次加息",
        "eventType": "市场动态", "sentiment": "bearish",
        "industryChange": "市场下跌", "changePercentage": -3.5,
    }]


def _mock_investment(conclusion: str = "银行板块受益，中期景气改善") -> dict[str, object]:
    return {
        "conclusion": conclusion,
        "keyPoints": ["息差扩大"],
        "focusIndustries": [{"name": "银行", "direction": "positive", "reason": "直接受益"}],
        "opportunities": ["银行股"],
        "risks": ["经济下行风险"],
        "rating": "positive",
    }


def _make_llm_mock(content: str) -> MagicMock:
    mock_llm = MagicMock()
    mock_result = MagicMock()
    mock_result.content = content
    mock_llm.ainvoke = AsyncMock(return_value=mock_result)
    return mock_llm


def _make_react_agent_mock(content: str) -> MagicMock:
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content=content)]})
    return mock_agent


# ── 公共 mock helper：单次 run() 所需全部外部依赖 ──


def _mock_run(
    understanding: dict[str, object] | None = None,
    transmission: dict[str, object] | None = None,
    history: list[object] | None = None,
    investment: dict[str, object] | None = None,
    podcast_text: str = "",
) -> tuple[ExitStack, AsyncMock, AsyncMock]:
    """用 ExitStack 统一 mock event agent run() 全部依赖。

    Returns:
        (stack, mock_persist, mock_set_cache): mock_persist 断言持久化调用；
        mock_set_cache 断言缓存写入。
    """
    stack = ExitStack()
    stack.enter_context(patch(
        _GET_CACHED_EVENT, new_callable=AsyncMock, return_value=None,
    ))
    stack.enter_context(patch(
        f"{_MODULE}._analyze_understanding",
        new_callable=AsyncMock, return_value=understanding,
    ))
    stack.enter_context(patch(
        f"{_MODULE}._analyze_transmission",
        new_callable=AsyncMock, return_value=transmission,
    ))
    stack.enter_context(patch(
        f"{_MODULE}._analyze_history",
        new_callable=AsyncMock, return_value=history,
    ))
    stack.enter_context(patch(
        f"{_MODULE}._analyze_investment",
        new_callable=AsyncMock, return_value=investment,
    ))
    stack.enter_context(patch(
        f"{_MODULE}._generate_podcast",
        new_callable=AsyncMock, return_value=podcast_text,
    ))
    mock_set_cache = stack.enter_context(
        patch(_SET_CACHED_EVENT, new_callable=AsyncMock),
    )
    mock_persist = stack.enter_context(
        patch(_PERSIST_EVENT_REPORT, new_callable=AsyncMock),
    )
    return stack, mock_persist, mock_set_cache


# ── run() orchestration tests ──


@pytest.mark.asyncio
async def test_run_no_user_message() -> None:
    result = await run({"messages": []})  # type: ignore[arg-type]
    assert result == {"final_response": "请提供需要分析的事件描述。", "analysis_reports": {}}


@pytest.mark.asyncio
async def test_run_no_human_message() -> None:
    result = await run({"messages": [AIMessage(content="ai msg")]})  # type: ignore[arg-type]
    assert result["final_response"] == "请提供需要分析的事件描述。"


@pytest.mark.asyncio
async def test_run_cache_hit() -> None:
    cached_data: dict[str, object] = {
        "event_understanding": {"summary": "缓存事件"},
        "event_transmission": {"mechanism": "缓存机制"},
        "event_history": [],
        "event_investment": {"conclusion": "缓存结论"},
        "event_podcast_brief": "缓存播报文本",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]

    mock_u.assert_not_called()
    assert result["final_response"] == "缓存播报文本"
    assert result["analysis_reports"]["event_podcast_brief"] == "缓存播报文本"


@pytest.mark.asyncio
async def test_run_understanding_failure() -> None:
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=None):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock, return_value=None):
            result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]
    assert result == {"final_response": "事件分析暂时不可用，请稍后重试", "analysis_reports": {}}


@pytest.mark.asyncio
async def test_run_full_success() -> None:
    """完整流程：5 个 LLM 调用全部成功，brief [150,200] 且完整持久化。"""
    brief_ok = "B" * 150
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    with s:
        result = await run({"messages": [HumanMessage(content="美联储加息影响")]})  # type: ignore[arg-type]

    assert result["final_response"] == brief_ok
    reports: dict[str, Any] = result["analysis_reports"]
    assert reports["event_podcast_brief"] == brief_ok
    assert reports["event_understanding"]["summary"] == "美联储加息25个基点"
    assert reports["event_transmission"]["mechanism"] == "加息提高资金成本"
    assert len(reports["event_history"]) == 1
    assert reports["event_investment"]["conclusion"] == "银行板块受益，中期景气改善"
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_run_exception_fallback() -> None:
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, side_effect=RuntimeError("Redis 不可用")):
        result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]
    assert result == {"final_response": "事件分析暂时不可用，请稍后重试", "analysis_reports": {}}


@pytest.mark.asyncio
async def test_run_partial_failure_transmission_none() -> None:
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=None,
        history=None,
        investment=_mock_investment(),
        podcast_text="A" * 150,
    )
    with s:
        result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]

    assert result["analysis_reports"]["event_transmission"] is None
    assert result["analysis_reports"]["event_history"] == []


# ── Helper function tests ──


@pytest.mark.asyncio
async def test_call_llm_no_tools_success() -> None:
    json_text = json.dumps({"summary": "测试", "coreChanges": []})
    mock_llm = _make_llm_mock(json_text)
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _call_llm_no_tools("system prompt", "user msg", model="flash")
    assert isinstance(result, dict)
    assert result["summary"] == "测试"


@pytest.mark.asyncio
async def test_call_llm_no_tools_failure() -> None:
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM 不可用"))
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _call_llm_no_tools("system prompt", "user msg", model="flash")
    assert result is None


@pytest.mark.asyncio
async def test_call_llm_with_tools_success() -> None:
    json_text = json.dumps({"mechanism": "测试传导"})
    mock_agent = _make_react_agent_mock(json_text)
    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _call_llm_with_tools("system prompt", "user msg", model="flash")
    assert isinstance(result, dict)
    assert result["mechanism"] == "测试传导"


@pytest.mark.asyncio
async def test_call_llm_with_tools_failure() -> None:
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(side_effect=RuntimeError("Agent 不可用"))
    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _call_llm_with_tools("system prompt", "user msg", model="flash")
    assert result is None


@pytest.mark.asyncio
async def test_call_llm_with_tools_binds_event_tools() -> None:
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
    json_text = json.dumps({"summary": "测试", "coreChanges": []})
    mock_llm = _make_llm_mock(json_text)
    with patch(_GET_QUICK_THINK, return_value=mock_llm) as mock_quick:
        with patch(_GET_DEEP_THINK, return_value=MagicMock()) as mock_deep:
            await _analyze_understanding("测试事件")
    mock_quick.assert_called_once()
    mock_deep.assert_not_called()


@pytest.mark.asyncio
async def test_analyze_transmission_uses_deep() -> None:
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
    json_text = json.dumps([{"historyId": "h001", "year": "2023", "title": "测试"}])
    mock_agent = _make_react_agent_mock(json_text)
    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _analyze_history("测试事件", {"summary": "测试"})
    assert isinstance(result, list)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_analyze_history_dict_wrapped_to_list() -> None:
    json_text = json.dumps({"historyId": "h001", "year": "2023", "title": "测试"})
    mock_agent = _make_react_agent_mock(json_text)
    with patch(_GET_QUICK_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _analyze_history("测试事件", {"summary": "测试"})
    assert isinstance(result, list)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_analyze_investment_uses_replace_not_format() -> None:
    json_text = json.dumps({"conclusion": "测试结论", "rating": "positive"})
    mock_llm = _make_llm_mock(json_text)
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        understanding = {"summary": "测试"}
        transmission = {"mechanism": "测试"}
        history = [{"historyId": "h001"}]
        result = await _analyze_investment(understanding, transmission, history)
    assert isinstance(result, dict)
    assert result["conclusion"] == "测试结论"


@pytest.mark.asyncio
async def test_analyze_investment_none_inputs() -> None:
    json_text = json.dumps({"conclusion": "无足够数据", "rating": "neutral"})
    mock_llm = _make_llm_mock(json_text)
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _analyze_investment(None, None, None)
    assert isinstance(result, dict)


@pytest.mark.asyncio
async def test_generate_podcast_returns_text() -> None:
    mock_llm = _make_llm_mock("这是150字的播报摘要文本。")
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _generate_podcast({"summary": "测试"}, "测试结论")
    assert isinstance(result, str)
    assert "播报摘要" in result


@pytest.mark.asyncio
async def test_generate_podcast_failure_returns_empty() -> None:
    """P1: _generate_podcast LLM 异常 → 返回空字符串（非降级占位文本）。"""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(side_effect=RuntimeError("LLM 不可用"))
    with patch(_GET_QUICK_THINK, return_value=mock_llm):
        result = await _generate_podcast({"summary": "测试"}, "测试结论")
    assert result == ""


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


# ── P1: _validate_podcast_brief 总函数 边界测试 ──


def test_validate_brief_150_ok() -> None:
    brief, ok = _validate_podcast_brief("A" * 150, None, "")
    assert ok is True
    assert len(brief) == 150


def test_validate_brief_200_ok() -> None:
    brief, ok = _validate_podcast_brief("A" * 200, None, "")
    assert ok is True
    assert len(brief) == 200


def test_validate_brief_149_padded_from_facts() -> None:
    """149 字 + 有 summary → 用事实补足到 ≥150。"""
    brief, ok = _validate_podcast_brief("A" * 149, {"summary": "美联储加息"}, "银行受益")
    assert ok is True
    assert 150 <= len(brief) <= 200


def test_validate_brief_149_no_facts_unpersistable() -> None:
    """149 字 + 空 summary + 空 conclusion → 无法补足，不可持久化。"""
    brief, ok = _validate_podcast_brief("A" * 149, None, "")
    assert ok is False
    assert len(brief) == 149  # 无法扩充


def test_validate_brief_empty_no_facts_unpersistable() -> None:
    """空 brief + 空 summary + 空 conclusion → 不可持久化。"""
    brief, ok = _validate_podcast_brief("", None, "")
    assert ok is False
    assert len(brief) == 0


def test_validate_brief_empty_with_facts_still_unpersistable() -> None:
    """空 brief + 任何 realistic summary + conclusion → 无法构造 150 字。

    summary 和 conclusion 是短结构化字段（各 ≤40 字），无法单独合成完整播报。
    这是正确行为——不得用泛型免责声明填充，不可持久化。
    """
    brief, ok = _validate_podcast_brief("", {"summary": "美联储加息25个基点"}, "银行板块受益")
    assert ok is False
    assert len(brief) < 150


def test_validate_brief_201_sentence_boundary() -> None:
    """201 字，在句子边界截断。无句子边界时按 max_len 截断。"""
    brief, ok = _validate_podcast_brief("A" * 201, None, "")
    assert len(brief) <= 200
    # 纯 "A" * 200 无句子边界，_truncate_at_sentence_boundary 返回全 200 字
    if len(brief) >= 150:
        assert ok is True
    else:
        assert ok is False


def test_truncate_at_sentence_end() -> None:
    """句子边界截断：在最后一个句号处截断。"""
    text = "事件摘要。" + "B" * 195 + "。多余文本"
    result = _truncate_at_sentence_boundary(text, 200)
    assert len(result) <= 200
    assert result.endswith("。") or result.endswith("B")


# ── P1: Title 元数据清洁 集成测试 ──


@pytest.mark.asyncio
async def test_run_title_from_understanding_summary() -> None:
    """title 来自 understanding.summary，不含用户指令前缀。"""
    user_msg = "请分析以下事件：美联储宣布降息25个基点，全球市场应声上涨"
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding("美联储降息25个基点"),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="A" * 150,
    )
    with s:
        await run({"messages": [HumanMessage(content=user_msg)]})

    mock_persist.assert_called_once()
    title = str(mock_persist.call_args.args[1].get("title", ""))
    assert "请分析以下" not in title, f"title 含指令前缀: '{title}'"
    assert "降息" in title, f"title 无事件关键词: '{title}'"


@pytest.mark.asyncio
async def test_run_title_no_summary_degrades_to_empty() -> None:
    """P1: understanding 无 summary → title 降级为空，不持久化也不缓存。"""
    understanding_no_summary: dict[str, object] = {"coreChanges": []}
    s, mock_persist, mock_set_cache = _mock_run(
        understanding=understanding_no_summary,
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="A" * 150,
    )
    with s:
        await run({"messages": [HumanMessage(content="帮我分析一下这个重大事件")]})

    mock_persist.assert_not_called(), "空 title 不应持久化"
    mock_set_cache.assert_not_called(), "空 title 不应缓存"


@pytest.mark.asyncio
async def test_run_unpersistable_not_cached() -> None:
    """P1: can_persist=False → 既不持久化也不写 Redis 缓存。"""
    understanding_no_summary: dict[str, object] = {"coreChanges": []}
    s, mock_persist, mock_set_cache = _mock_run(
        understanding=understanding_no_summary,
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(""),
        podcast_text="A" * 149,  # too short + no facts to pad
    )
    with s:
        await run({"messages": [HumanMessage(content="测试事件")]})

    mock_persist.assert_not_called(), "can_persist=False 不应持久化"
    mock_set_cache.assert_not_called(), "can_persist=False 不应缓存"


@pytest.mark.asyncio
async def test_run_title_no_event_prefix() -> None:
    """title 不含 '事件：' 前缀。"""
    user_msg = "事件：天际股份因子公司财务造假被ST"
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding("天际股份因子公司财务造假被ST"),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="A" * 150,
    )
    with s:
        await run({"messages": [HumanMessage(content=user_msg)]})

    mock_persist.assert_called_once()
    title = str(mock_persist.call_args.args[1].get("title", ""))
    assert not title.startswith("事件："), f"title 不应以 '事件：' 开头: '{title}'"
    assert "天际股份" in title


# ── P1: podcast_brief 总函数 + 不可持久化 集成测试 ──


@pytest.mark.asyncio
async def test_run_brief_149_empty_facts_not_persisted() -> None:
    """P1: 149 字 brief + 空 summary + 空 conclusion → 不持久化为 completed。"""
    understanding_no_summary: dict[str, object] = {"coreChanges": []}
    s, mock_persist, _ = _mock_run(
        understanding=understanding_no_summary,
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(""),  # 空 conclusion
        podcast_text="A" * 149,
    )
    with s:
        await run({"messages": [HumanMessage(content="测试事件")]})

    mock_persist.assert_not_called(), "149 字+无事实不应持久化"


@pytest.mark.asyncio
async def test_run_brief_empty_empty_facts_not_persisted() -> None:
    """P1: 空 brief + 空 summary + 空 conclusion → 不持久化。"""
    understanding_no_summary: dict[str, object] = {"coreChanges": []}
    s, mock_persist, _ = _mock_run(
        understanding=understanding_no_summary,
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(""),
        podcast_text="",
    )
    with s:
        await run({"messages": [HumanMessage(content="测试事件")]})

    mock_persist.assert_not_called(), "空 brief+无事实不应持久化"


@pytest.mark.asyncio
async def test_run_brief_150_ok_persisted() -> None:
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="A" * 150,
    )
    with s:
        await run({"messages": [HumanMessage(content="测试事件")]})
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_run_brief_200_ok_persisted() -> None:
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="A" * 200,
    )
    with s:
        await run({"messages": [HumanMessage(content="测试事件")]})
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_run_brief_201_truncated_and_persisted() -> None:
    """201 字 → 句子边界截断到 200 → 持久化。"""
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="A" * 201,
    )
    with s:
        result = await run({"messages": [HumanMessage(content="测试事件")]})
    mock_persist.assert_called_once()
    brief = str(result["analysis_reports"].get("event_podcast_brief", ""))
    assert len(brief) <= 200
    assert len(brief) >= 150


@pytest.mark.asyncio
async def test_run_brief_padded_from_understanding_facts() -> None:
    """149 字 + 有 summary → 从事实补足后持久化。"""
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding("美联储加息25个基点"),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment("银行板块受益"),
        podcast_text="A" * 149,
    )
    with s:
        result = await run({"messages": [HumanMessage(content="测试事件")]})
    mock_persist.assert_called_once()
    brief = str(result["analysis_reports"].get("event_podcast_brief", ""))
    assert 150 <= len(brief) <= 200, f"实际: {len(brief)}"
