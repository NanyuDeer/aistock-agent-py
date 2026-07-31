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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.tools import BaseTool

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
from aistock_agent.services.data_client import IndustryChainReadResult
from aistock_agent.utils.output_parser import transform_to_frontend

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
    "get_industry_chain",
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


def _verified_cached_transmission() -> dict[str, object]:
    """可复用缓存必须带有可审计的 IndustryKG 一跳事实。"""
    return {
        **_mock_transmission(),
        "industry_graph_boundary_version": "one_hop_v1",
        "industryGraphEvidence": [
            {
                "status": "found",
                "degraded": False,
                "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "bank", "name": "银行"},
                "upstream": [],
                "downstream": [],
                "graphVersion": "kg-v1",
                "updatedAt": "2026-07-25T00:00:00Z",
            }
        ],
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


class _ToolInvokingAgent:
    """测试用 ReAct agent：实际调用传入的 IndustryKG 工具。"""

    def __init__(self, tools: list[BaseTool], final_content: str) -> None:
        self._tools = {tool.name: tool for tool in tools}
        self._final_content = final_content

    async def ainvoke(self, _input: object) -> dict[str, object]:
        evidence = await self._tools["get_industry_chain"].ainvoke(
            {"industry_name": "半导体"}
        )
        return {
            "messages": [
                ToolMessage(
                    content=evidence,
                    tool_call_id="kg-1",
                    name="get_industry_chain",
                ),
                AIMessage(content=self._final_content),
            ]
        }


class _FinalOnlyAgent:
    """测试用 ReAct agent：只给最终模型文本，不产生工具消息。"""

    def __init__(self, final_content: str) -> None:
        self._final_content = final_content

    async def ainvoke(self, _input: object) -> dict[str, object]:
        return {"messages": [AIMessage(content=self._final_content)]}


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
        patch(_SET_CACHED_EVENT, new_callable=AsyncMock, return_value=True),
    )
    mock_persist = stack.enter_context(
        patch(_PERSIST_EVENT_REPORT, new_callable=AsyncMock),
    )
    return stack, mock_persist, mock_set_cache


# ── run() orchestration tests ──


@pytest.mark.asyncio
async def test_run_no_user_message() -> None:
    result = await run({"messages": []})  # type: ignore[arg-type]
    assert result["final_response"] == "请提供需要分析的事件描述。"
    assert result["analysis_reports"]["event_generated"] is False
    assert result["analysis_reports"]["event_persisted"] is False
    assert result["analysis_reports"]["event_cached"] is False


@pytest.mark.asyncio
async def test_run_no_human_message() -> None:
    result = await run({"messages": [AIMessage(content="ai msg")]})  # type: ignore[arg-type]
    assert result["final_response"] == "请提供需要分析的事件描述。"


@pytest.mark.asyncio
async def test_run_cache_hit() -> None:
    cached_data: dict[str, object] = {
        "event_understanding": {"summary": "缓存事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_history": [],
        "event_investment": {"conclusion": "缓存结论"},
        "event_podcast_brief": "缓存播报文本",
        "event_generated": True,
        "event_persisted": True,
        "event_id": "evt_cached1",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]

    mock_u.assert_not_called()
    assert result["final_response"] == "缓存播报文本"
    assert result["analysis_reports"]["event_podcast_brief"] == "缓存播报文本"
    assert result["analysis_reports"]["event_cached"] is True
    assert result["analysis_reports"]["event_generated"] is True


@pytest.mark.asyncio
async def test_run_regenerates_cache_without_verifiable_graph_boundary() -> None:
    """旧缓存缺少一跳版本或 found 证据时不得复用其派生结论。"""
    cached_data: dict[str, object] = {
        "event_understanding": {"summary": "缓存事件"},
        "event_transmission": {
            "eventId": "evt_legacy_kg",
            "mechanism": "缓存机制",
            "variables": [],
            "coreIndustry": {"name": "半导体", "impact": "", "reason": ""},
            "chain": [
                {
                    "industry": "半导体",
                    "relation": "核心行业",
                    "level": 1,
                    "direction": "bullish",
                    "impactStrength": 0.7,
                    "reason": "事件变量推断",
                },
                {
                    "industry": "旧缓存虚构行业",
                    "relation": "上游传导",
                    "level": 2,
                    "direction": "bearish",
                    "impactStrength": 0.8,
                    "reason": "旧模型输出",
                },
            ],
        },
        "event_history": [],
        "event_investment": {"conclusion": "缓存结论"},
        "event_podcast_brief": "缓存播报文本",
        "event_generated": True,
        "event_persisted": True,
        "event_id": "evt_legacy_kg",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(
            f"{_MODULE}._analyze_understanding",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_u:
            result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]

    mock_u.assert_awaited_once()
    assert result["final_response"] == "事件分析暂时不可用，请稍后重试"
    assert result["analysis_reports"]["event_cached"] is False


@pytest.mark.asyncio
async def test_run_understanding_failure() -> None:
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=None):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock, return_value=None):
            result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]
    assert result["final_response"] == "事件分析暂时不可用，请稍后重试"
    assert result["analysis_reports"]["event_generated"] is False
    assert result["analysis_reports"]["event_persisted"] is False
    assert result["analysis_reports"]["event_cached"] is False


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
    assert reports["event_transmission"]["industry_graph_boundary_version"] == "one_hop_v1"
    assert len(reports["event_history"]) == 1
    assert reports["event_investment"]["conclusion"] == "银行板块受益，中期景气改善"
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_run_constrains_graph_chain_before_downstream_derivations() -> None:
    """投资、播报和持久化只能消费真实一跳证据约束后的传导链。"""
    raw_transmission = {
        **_mock_transmission(),
        "chain": [
            {"industry": "半导体", "relation": "核心行业", "level": 1},
            {"industry": "电子化学品", "relation": "上游传导", "level": 2},
            {"industry": "钢铁", "relation": "上游传导", "level": 2},
        ],
        "industryGraphEvidence": [
            {
                "status": "found", "degraded": False, "scope": "one_hop",
                "source": "IndustryKGService",
                "industry": {"id": "semi", "name": "半导体"},
                "upstream": [{"id": "chem", "name": "电子化学品", "leadingStocks": []}],
                "downstream": [],
            }
        ],
    }

    async def investment_from_constrained_chain(
        _understanding: dict[str, object] | None,
        transmission: dict[str, object] | None,
        _history: list[object] | None,
    ) -> dict[str, object]:
        assert transmission is not None
        assert [item["industry"] for item in transmission["chain"]] == [
            "半导体", "电子化学品"
        ]
        return _mock_investment("仅基于一跳事实的结论")

    brief = "B" * 150
    with ExitStack() as stack:
        stack.enter_context(
            patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=None)
        )
        stack.enter_context(
            patch(
                f"{_MODULE}._analyze_understanding",
                new_callable=AsyncMock,
                return_value=_mock_understanding(),
            )
        )
        stack.enter_context(
            patch(
                f"{_MODULE}._analyze_transmission",
                new_callable=AsyncMock,
                return_value=raw_transmission,
            )
        )
        stack.enter_context(
            patch(
                f"{_MODULE}._analyze_history",
                new_callable=AsyncMock,
                return_value=_mock_history(),
            )
        )
        stack.enter_context(
            patch(
                f"{_MODULE}._analyze_investment",
                new_callable=AsyncMock,
                side_effect=investment_from_constrained_chain,
            )
        )
        stack.enter_context(
            patch(f"{_MODULE}._generate_podcast", new_callable=AsyncMock, return_value=brief)
        )
        mock_persist = stack.enter_context(
            patch(_PERSIST_EVENT_REPORT, new_callable=AsyncMock, return_value=True)
        )
        stack.enter_context(
            patch(_SET_CACHED_EVENT, new_callable=AsyncMock, return_value=True)
        )
        result = await run({"messages": [HumanMessage(content="测试事件")]})

    persisted = mock_persist.await_args.args[3]
    expected_chain = ["半导体", "电子化学品"]
    persisted_chain = persisted["event_transmission"]["chain"]
    report_chain = result["analysis_reports"]["event_transmission"]["chain"]
    assert [item["industry"] for item in persisted_chain] == expected_chain
    assert [item["industry"] for item in report_chain] == expected_chain


@pytest.mark.asyncio
async def test_run_exception_fallback() -> None:
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, side_effect=RuntimeError("Redis 不可用")):
        result = await run({"messages": [HumanMessage(content="测试事件")]})  # type: ignore[arg-type]
    assert result["final_response"] == "事件分析暂时不可用，请稍后重试"
    assert result["analysis_reports"]["event_generated"] is False
    assert result["analysis_reports"]["event_persisted"] is False
    assert result["analysis_reports"]["event_cached"] is False


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
    assert result is not None
    assert isinstance(result.parsed, dict)
    assert result.parsed["mechanism"] == "测试传导"
    assert result.industry_graph_evidence == []


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
async def test_analyze_transmission_captures_real_industry_graph_tool_evidence() -> None:
    """Transmission 必须消费实际工具调用产生的 JSON 图谱证据。"""
    final_content = json.dumps(_mock_transmission(), ensure_ascii=False)
    read_result = IndustryChainReadResult(
        "found",
        {
            "industry": {"id": "881121.TI", "name": "半导体"},
            "upstream": [],
            "downstream": [],
            "graphVersion": "kg-2026-07-22",
            "updatedAt": "2026-07-22T09:00:00Z",
        },
        "IndustryKGService",
    )

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(
            "aistock_agent.agents.workers.event.create_react_agent",
            side_effect=lambda _llm, tools: _ToolInvokingAgent(tools, final_content),
        ):
            with patch(
                "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
                new=AsyncMock(return_value=read_result),
            ) as mock_get_industry_chain:
                result = await _analyze_transmission(
                    "半导体需求增长", _mock_understanding()
                )

    assert isinstance(result, dict)
    evidence = result["industryGraphEvidence"]
    assert isinstance(evidence, list)
    assert evidence[0]["status"] == "found"
    assert evidence[0]["source"] == "IndustryKGService"
    mock_get_industry_chain.assert_awaited_once_with("半导体")


@pytest.mark.asyncio
async def test_analyze_transmission_does_not_trust_llm_graph_evidence_without_tool_call() -> None:
    """没有 ToolMessage 时，模型自称已验证的图谱事实必须被替换为 not_queried。"""
    transmission = _mock_transmission()
    transmission["industryGraphEvidence"] = [{"status": "found", "degraded": False}]
    final_content = json.dumps(transmission, ensure_ascii=False)

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(
            "aistock_agent.agents.workers.event.create_react_agent",
            return_value=_FinalOnlyAgent(final_content),
        ):
            result = await _analyze_transmission("测试事件", _mock_understanding())

    assert isinstance(result, dict)
    evidence = result["industryGraphEvidence"]
    assert isinstance(evidence, list)
    assert evidence[0]["status"] == "not_queried"
    assert evidence[0]["degraded"] is True


@pytest.mark.asyncio
async def test_analyze_transmission_marks_graph_not_queried_when_react_fails() -> None:
    """整个 ReAct 调用失败时必须保留未查询边界且不生成链路。"""
    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, side_effect=RuntimeError("Agent 不可用")):
            transmission = await _analyze_transmission("测试事件", _mock_understanding())

    reports = transform_to_frontend(
        _mock_understanding(),
        transmission,
        [],
        None,
        {"eventId": "evt_react_error", "title": "测试", "source": ""},
    )
    mapped = reports["event_transmission"]
    assert isinstance(mapped, dict)
    assert mapped["industryGraphEvidence"][0]["status"] == "not_queried"
    assert mapped["industryGraphEvidence"][0]["degraded"] is True
    assert mapped["chain"] == []


@pytest.mark.asyncio
async def test_analyze_transmission_ignores_malformed_industry_graph_tool_message() -> None:
    """格式错误的图谱工具内容必须显式标记为无效响应。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                ToolMessage(
                    content="not-json",
                    tool_call_id="kg-invalid",
                    name="get_industry_chain",
                ),
                AIMessage(content=json.dumps(_mock_transmission(), ensure_ascii=False)),
            ]
        }
    )

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _analyze_transmission("测试事件", _mock_understanding())

    assert isinstance(result, dict)
    evidence = result["industryGraphEvidence"]
    assert isinstance(evidence, list)
    assert evidence[0]["status"] == "invalid_response"
    assert evidence[0]["degraded"] is True
    assert evidence[0]["missingBoundary"]


@pytest.mark.asyncio
async def test_analyze_transmission_marks_empty_graph_tool_json_as_invalid_response() -> None:
    """合法 JSON 但缺少图谱证据骨架时不能被视为已查询事实。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                ToolMessage(
                    content="{}",
                    tool_call_id="kg-empty",
                    name="get_industry_chain",
                ),
                AIMessage(content=json.dumps(_mock_transmission(), ensure_ascii=False)),
            ]
        }
    )

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _analyze_transmission("测试事件", _mock_understanding())

    assert isinstance(result, dict)
    evidence = result["industryGraphEvidence"]
    assert isinstance(evidence, list)
    assert evidence[0]["status"] == "invalid_response"
    assert evidence[0]["degraded"] is True
    assert evidence[0]["missingBoundary"]


@pytest.mark.asyncio
async def test_analyze_transmission_rejects_found_graph_evidence_with_malformed_nodes() -> None:
    """found 图谱证据缺少行业节点身份字段时必须降级。"""
    forged_evidence = {
        "status": "found",
        "degraded": False,
        "scope": "one_hop",
        "source": "IndustryKGService",
        "industry": {},
        "upstream": [{"name": "伪造行业"}],
        "downstream": [],
    }
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                ToolMessage(
                    content=json.dumps(forged_evidence, ensure_ascii=False),
                    tool_call_id="kg-forged",
                    name="get_industry_chain",
                ),
                AIMessage(content=json.dumps(_mock_transmission(), ensure_ascii=False)),
            ]
        }
    )

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            result = await _analyze_transmission("测试事件", _mock_understanding())

    assert isinstance(result, dict)
    evidence = result["industryGraphEvidence"]
    assert isinstance(evidence, list)
    assert evidence[0]["status"] == "invalid_response"
    assert evidence[0]["degraded"] is True
    assert evidence[0]["missingBoundary"]


@pytest.mark.asyncio
async def test_analyze_transmission_preserves_degraded_tool_evidence_when_invalid_json() -> None:
    """最终 AI JSON 无效时仍保留真实工具降级证据，且不伪造链路。"""
    tool_evidence = {
        "status": "authentication_failed",
        "degraded": True,
        "scope": "one_hop",
        "source": None,
        "industry": None,
        "upstream": None,
        "downstream": None,
        "graphVersion": None,
        "updatedAt": None,
        "missingBoundary": "鉴权失败，未取得图谱事实。",
    }
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                ToolMessage(
                    content=json.dumps(tool_evidence, ensure_ascii=False),
                    tool_call_id="kg-auth-failed",
                    name="get_industry_chain",
                ),
                AIMessage(content="not-json"),
            ]
        }
    )

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(_CREATE_REACT_AGENT, return_value=mock_agent):
            transmission = await _analyze_transmission("测试事件", _mock_understanding())

    reports = transform_to_frontend(
        _mock_understanding(),
        transmission,
        [],
        None,
        {"eventId": "evt_auth", "title": "测试", "source": ""},
    )
    mapped = reports["event_transmission"]
    assert isinstance(mapped, dict)
    assert mapped["industryGraphEvidence"][0]["status"] == "authentication_failed"
    assert mapped["industryGraphEvidence"][0]["missingBoundary"] == "鉴权失败，未取得图谱事实。"
    assert mapped["chain"] == []


@pytest.mark.asyncio
async def test_transform_to_frontend_keeps_only_core_chain_when_graph_is_degraded() -> None:
    """图谱降级时，模型虚构的非核心行业不得进入前端链路。"""
    final_content = json.dumps(
        {
            **_mock_transmission(),
            "chain": [
                {
                    "industry": "银行",
                    "relation": "核心行业",
                    "level": 1,
                    "direction": "bullish",
                    "impactStrength": 0.7,
                    "reason": "事件变量推断",
                },
                {
                    "industry": "虚构行业",
                    "relation": "上游传导",
                    "level": 2,
                    "direction": "bearish",
                    "impactStrength": 0.9,
                    "reason": "模型补造",
                },
            ],
        },
        ensure_ascii=False,
    )

    with patch(_GET_DEEP_THINK, return_value=MagicMock()):
        with patch(
            "aistock_agent.agents.workers.event.create_react_agent",
            side_effect=lambda _llm, tools: _ToolInvokingAgent(tools, final_content),
        ):
            with patch(
                "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
                new=AsyncMock(
                    return_value=IndustryChainReadResult("authentication_failed")
                ),
            ):
                transmission = await _analyze_transmission(
                    "半导体需求增长", _mock_understanding()
                )

    reports = transform_to_frontend(
        _mock_understanding(),
        transmission,
        [],
        None,
        {"eventId": "evt_kg", "title": "测试", "source": ""},
    )
    mapped = reports["event_transmission"]
    assert isinstance(mapped, dict)
    evidence = mapped["industryGraphEvidence"]
    assert isinstance(evidence, list)
    assert evidence[0]["status"] == "authentication_failed"
    assert evidence[0]["degraded"] is True
    chain = mapped["chain"]
    assert isinstance(chain, list)
    assert all(item["relation"] == "核心行业" for item in chain)
    assert all(item["level"] == 1 for item in chain)
    assert {item["industry"] for item in chain} == {"银行"}


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


# ── dict message 契约测试（scheduler / 手动入口传 dict，非 HumanMessage）──


@pytest.mark.asyncio
async def test_run_dict_message_cache_hit() -> None:
    """dict message（{"role":"user","content":"..."}）也应命中缓存。"""
    cached_data: dict[str, object] = {
        "event_podcast_brief": "缓存播报文本",
        "event_understanding": {"summary": "缓存事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_generated": True,
        "event_persisted": True,
        "event_id": "evt_dict_cache",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            result = await run(
                {"messages": [{"role": "user", "content": "美联储加息"}]}  # type: ignore[arg-type]
            )

    mock_u.assert_not_called()
    assert result["final_response"] == "缓存播报文本"
    assert result["analysis_reports"]["event_cached"] is True


@pytest.mark.asyncio
async def test_run_dict_message_full_success() -> None:
    """dict message 也应走完整 5 步 LLM 流程并持久化。"""
    brief_ok = "B" * 150
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    with s:
        result = await run(
            {"messages": [{"role": "user", "content": "美联储加息影响"}]}  # type: ignore[arg-type]
        )

    assert result["final_response"] == brief_ok
    mock_persist.assert_called_once()


@pytest.mark.asyncio
async def test_run_dict_message_wrong_role_returns_empty() -> None:
    """dict message role != 'user' → 空消息降级。"""
    result = await run(
        {"messages": [{"role": "assistant", "content": "AI 回复"}]}  # type: ignore[arg-type]
    )
    assert result["final_response"] == "请提供需要分析的事件描述。"


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


# ── 显式状态契约测试（event_generated/event_persisted/event_cached/event_id）──


@pytest.mark.asyncio
async def test_run_full_success_has_explicit_status() -> None:
    """成功路径必须提供显式状态：event_generated=True, event_persisted, event_cached, event_id。"""
    brief_ok = "B" * 150
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding("美联储加息"),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    mock_persist.return_value = True
    with s:
        result = await run({"messages": [HumanMessage(content="美联储加息影响")]})

    analysis_reports = result["analysis_reports"]
    assert analysis_reports["event_generated"] is True, "成功路径 event_generated 必须为 True"
    assert analysis_reports["event_persisted"] is True
    assert analysis_reports["event_cached"] is True
    assert isinstance(analysis_reports.get("event_id"), str)
    assert analysis_reports["event_id"].startswith("evt_")
    # 禁止存在 event_display_report 虚构字段
    assert "event_display_report" not in analysis_reports


@pytest.mark.asyncio
async def test_run_cache_hit_has_explicit_status() -> None:
    """缓存命中路径必须提供显式状态：event_cached=True, event_id。"""
    cached_data: dict[str, object] = {
        "event_podcast_brief": "缓存播报文本",
        "event_understanding": {"summary": "缓存事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_id": "evt_cached123",
        "event_persisted": True,
        "event_generated": True,
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            result = await run(
                {"messages": [{"role": "user", "content": "美联储加息"}]}  # type: ignore[arg-type]
            )

    analysis_reports = result["analysis_reports"]
    assert analysis_reports["event_cached"] is True
    assert analysis_reports["event_generated"] is True
    assert analysis_reports["event_id"] == "evt_cached123"
    mock_u.assert_not_called()


@pytest.mark.asyncio
async def test_run_degraded_understanding_failure_has_explicit_status() -> None:
    """understanding 失败降级路径必须提供显式状态：event_generated=False。"""
    s, _, _ = _mock_run(
        understanding=None,
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="A" * 150,
    )
    with s:
        result = await run({"messages": [HumanMessage(content="测试事件")]})

    analysis_reports = result["analysis_reports"]
    assert analysis_reports["event_generated"] is False
    assert analysis_reports["event_persisted"] is False
    assert analysis_reports["event_cached"] is False
    assert "event_id" in analysis_reports


@pytest.mark.asyncio
async def test_run_exception_has_explicit_status() -> None:
    """异常路径必须提供显式状态：event_generated=False。"""
    with patch(
        f"{_MODULE}._analyze_understanding",
        new_callable=AsyncMock,
        side_effect=RuntimeError("LLM 不可用"),
    ):
        result = await run({"messages": [HumanMessage(content="测试事件")]})

    analysis_reports = result["analysis_reports"]
    assert analysis_reports["event_generated"] is False
    assert analysis_reports["event_persisted"] is False
    assert analysis_reports["event_cached"] is False


@pytest.mark.asyncio
async def test_run_empty_message_has_explicit_status() -> None:
    """空消息降级路径必须提供显式状态：event_generated=False。"""
    result = await run({"messages": []})

    analysis_reports = result["analysis_reports"]
    assert analysis_reports["event_generated"] is False
    assert analysis_reports["event_persisted"] is False
    assert analysis_reports["event_cached"] is False


@pytest.mark.asyncio
async def test_run_persist_failure_reports_not_persisted() -> None:
    """持久化失败时 event_persisted=False，但 event_generated=True。"""
    brief_ok = "B" * 150
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding("美联储加息"),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    mock_persist.return_value = False
    with s:
        result = await run({"messages": [HumanMessage(content="美联储加息影响")]})

    analysis_reports = result["analysis_reports"]
    assert analysis_reports["event_generated"] is True
    assert analysis_reports["event_persisted"] is False
    assert analysis_reports["event_cached"] is True  # 缓存仍写入


@pytest.mark.asyncio
async def test_run_analysis_reports_has_real_production_structure() -> None:
    """成功路径 analysis_reports 必须包含真实生产结构字段，禁止依赖 event_display_report。"""
    brief_ok = "B" * 150
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    mock_persist.return_value = True
    with s:
        result = await run({"messages": [HumanMessage(content="美联储加息")]})

    analysis_reports = result["analysis_reports"]
    # 真实生产结构字段
    assert "event_understanding" in analysis_reports
    assert "event_transmission" in analysis_reports
    assert "event_history" in analysis_reports
    assert "event_investment" in analysis_reports
    assert "event_podcast_brief" in analysis_reports
    # 禁止虚构字段
    assert "event_display_report" not in analysis_reports


# ── 缓存补偿测试：首次落库失败后缓存命中补写成功 ──


@pytest.mark.asyncio
async def test_run_cache_hit_idempotent_repersist_after_failure() -> None:
    """首次落库失败 → 缓存命中时执行幂等补写，成功后 event_persisted=True。"""
    # 缓存中保存了首次生成结果但 event_persisted=False
    cached_data: dict[str, object] = {
        "event_podcast_brief": "缓存播报文本",
        "event_understanding": {"summary": "测试事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_generated": True,
        "event_persisted": False,  # 首次落库失败
        "event_id": "evt_retry1",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            with patch(
                f"{_MODULE}.persist_event_report",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_persist:
                with patch(f"{_MODULE}.set_cached_event", new_callable=AsyncMock) as mock_set_cache:
                    result = await run(
                        {"messages": [HumanMessage(content="测试事件")]}
                    )

    mock_u.assert_not_called()  # 缓存命中，不调 LLM
    mock_persist.assert_called_once()  # 幂等补写被触发
    assert result["analysis_reports"]["event_persisted"] is True
    assert result["analysis_reports"]["event_cached"] is True
    assert result["analysis_reports"]["event_generated"] is True
    # 缓存被更新为 persisted=True
    mock_set_cache.assert_called()


@pytest.mark.asyncio
async def test_run_cache_hit_repersist_still_fails() -> None:
    """幂等补写仍失败 → event_persisted=False，但不影响 event_generated=True。"""
    cached_data: dict[str, object] = {
        "event_podcast_brief": "缓存播报文本",
        "event_understanding": {"summary": "测试事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_generated": True,
        "event_persisted": False,
        "event_id": "evt_retry2",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            with patch(
                f"{_MODULE}.persist_event_report",
                new_callable=AsyncMock,
                return_value=False,
            ):
                result = await run(
                    {"messages": [HumanMessage(content="测试事件")]}
                )

    mock_u.assert_not_called()
    assert result["analysis_reports"]["event_persisted"] is False
    assert result["analysis_reports"]["event_generated"] is True
    assert result["analysis_reports"]["event_cached"] is True


@pytest.mark.asyncio
async def test_run_cache_hit_old_cache_without_persisted_field() -> None:
    """旧缓存无 event_persisted 字段 → 视为 False，触发幂等补写。"""
    cached_data: dict[str, object] = {
        "event_podcast_brief": "旧缓存播报文本",
        "event_understanding": {"summary": "旧事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_generated": True,
        # 注意：没有 event_persisted 字段
        "event_id": "evt_old1",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            with patch(
                f"{_MODULE}.persist_event_report",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_persist:
                result = await run(
                    {"messages": [HumanMessage(content="旧事件")]}
                )

    mock_u.assert_not_called()
    mock_persist.assert_called_once()  # 旧缓存无 persisted → 补写
    assert result["analysis_reports"]["event_persisted"] is True


@pytest.mark.asyncio
async def test_run_cache_hit_already_persisted_no_repersist() -> None:
    """缓存中 event_persisted=True → 不触发幂等补写。"""
    cached_data: dict[str, object] = {
        "event_podcast_brief": "已持久化缓存",
        "event_understanding": {"summary": "已持久化事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_generated": True,
        "event_persisted": True,  # 已持久化
        "event_id": "evt_done1",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            with patch(f"{_MODULE}.persist_event_report", new_callable=AsyncMock) as mock_persist:
                result = await run(
                    {"messages": [HumanMessage(content="已持久化事件")]}
                )

    mock_u.assert_not_called()
    mock_persist.assert_not_called()  # 已持久化，不补写
    assert result["analysis_reports"]["event_persisted"] is True


@pytest.mark.asyncio
async def test_run_cache_hit_degraded_cache_not_repersisted() -> None:
    """缓存中 event_generated=False（降级缓存）→ 不触发幂等补写。"""
    cached_data: dict[str, object] = {
        "event_podcast_brief": "降级缓存",
        "event_transmission": _verified_cached_transmission(),
        "event_generated": False,  # 降级
        "event_persisted": False,
        "event_id": "evt_deg1",
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            with patch(f"{_MODULE}.persist_event_report", new_callable=AsyncMock) as mock_persist:
                result = await run(
                    {"messages": [HumanMessage(content="降级事件")]}
                )

    mock_u.assert_not_called()
    mock_persist.assert_not_called()  # 降级缓存不补写
    assert result["analysis_reports"]["event_persisted"] is False
    assert result["analysis_reports"]["event_generated"] is False


# ── 旧缓存兼容（无运行时状态字段）+ event_generated/event_cached 状态准确性 ──


@pytest.mark.asyncio
async def test_run_rejects_legacy_cache_without_any_status_fields() -> None:
    """无审计一跳边界的旧缓存不得复用其 investment 或 podcast。"""
    legacy_cache: dict[str, object] = {
        "event_understanding": {"summary": "美联储紧急降息50基点"},
        "event_transmission": {"mechanism": "流动性宽松"},
        "event_history": [],
        "event_investment": {"conclusion": "风险资产受益"},
        "event_podcast_brief": "美联储紧急降息50基点，市场流动性大幅宽松，风险资产短期受益。",
        # 注意：以下字段一律不预置 —— event_generated/event_persisted/event_cached/event_id
    }
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=legacy_cache):
        with patch(
            f"{_MODULE}._analyze_understanding",
            new_callable=AsyncMock,
            return_value=None,
        ) as mock_u:
            with patch(
                f"{_MODULE}.persist_event_report",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_persist:
                with patch(
                    f"{_MODULE}.set_cached_event",
                    new_callable=AsyncMock,
                    return_value=True,
                ):
                    result = await run(
                        {"messages": [HumanMessage(content="美联储紧急降息50基点")]}
                    )

    mock_u.assert_awaited_once()
    mock_persist.assert_not_called()
    assert result["final_response"] == "事件分析暂时不可用，请稍后重试"
    assert result["analysis_reports"]["event_cached"] is False


@pytest.mark.asyncio
async def test_run_empty_title_event_generated_false() -> None:
    """标题为空（understanding 无 summary）时 event_generated 必须为 False，
    不得计入生成成功（即使 brief 合规）。"""
    understanding_no_summary: dict[str, object] = {"coreChanges": []}
    s, mock_persist, _ = _mock_run(
        understanding=understanding_no_summary,
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text="B" * 150,  # brief 合规，但 title 为空
    )
    with s:
        result = await run({"messages": [HumanMessage(content="某重大事件")]})

    analysis_reports = result["analysis_reports"]
    # 标题为空 → event_generated=False（即使 brief 合规）
    assert analysis_reports["event_generated"] is False
    assert analysis_reports["event_persisted"] is False
    assert analysis_reports["event_cached"] is False
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_run_invalid_podcast_event_generated_false() -> None:
    """播报校验失败（brief 过短且无可扩充事实）→ event_generated 必须为 False，
    不得计入生成成功。"""
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding("某事件"),  # title 非空
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(""),
        podcast_text="A" * 10,  # 远低于 150，且 conclusion 为空难以扩充
    )
    with s:
        result = await run({"messages": [HumanMessage(content="某重大事件")]})

    analysis_reports = result["analysis_reports"]
    # 播报校验失败 → event_generated=False
    assert analysis_reports["event_generated"] is False
    assert analysis_reports["event_persisted"] is False
    assert analysis_reports["event_cached"] is False
    mock_persist.assert_not_called()


@pytest.mark.asyncio
async def test_run_cache_write_failure_event_cached_false() -> None:
    """set_cached_event 返回 False（Redis 写入失败）→ event_cached=False，
    即使后续持久化成功。覆盖 event_cached 写入失败场景。"""
    brief_ok = "B" * 150
    s, mock_persist, mock_set_cache = _mock_run(
        understanding=_mock_understanding("美联储加息"),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    mock_set_cache.return_value = False  # Redis 写入失败
    mock_persist.return_value = True
    with s:
        result = await run({"messages": [HumanMessage(content="美联储加息影响")]})

    analysis_reports = result["analysis_reports"]
    # 报告结构有效、brief 合规、title 非空 → event_generated=True
    assert analysis_reports["event_generated"] is True
    # 持久化成功
    assert analysis_reports["event_persisted"] is True
    # 但缓存写入失败 → event_cached=False
    assert analysis_reports["event_cached"] is False


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


# ── 来源元数据传导：event_source → event_meta.source ──


@pytest.mark.asyncio
async def test_run_event_source_from_state_in_event_meta() -> None:
    """event_source 从初始 state.analysis_reports 传入 event_meta.source，
    持久化时携带真实来源 URL（而非硬编码空字符串）。"""
    brief_ok = "B" * 150
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    source_url = "https://news.example.com/fed-rate-hike"
    with s:
        await run({
            "messages": [HumanMessage(content="美联储加息影响")],
            "analysis_reports": {"event_source": source_url},
        })  # type: ignore[arg-type]

    mock_persist.assert_called_once()
    event_meta = mock_persist.call_args.args[1]
    assert event_meta.get("source") == source_url


@pytest.mark.asyncio
async def test_run_no_event_source_defaults_empty() -> None:
    """初始 state 无 event_source → event_meta.source 为空字符串（兼容旧调用方）。"""
    brief_ok = "B" * 150
    s, mock_persist, _ = _mock_run(
        understanding=_mock_understanding(),
        transmission=_mock_transmission(),
        history=_mock_history(),
        investment=_mock_investment(),
        podcast_text=brief_ok,
    )
    with s:
        await run({
            "messages": [HumanMessage(content="美联储加息影响")],
        })  # type: ignore[arg-type]

    mock_persist.assert_called_once()
    event_meta = mock_persist.call_args.args[1]
    assert event_meta.get("source") == ""


@pytest.mark.asyncio
async def test_run_cache_hit_repersist_uses_event_source() -> None:
    """缓存命中幂等补写时，cached_meta.source 也从初始 state 读取真实来源。"""
    cached_data: dict[str, object] = {
        "event_podcast_brief": "缓存播报文本",
        "event_understanding": {"summary": "测试事件"},
        "event_transmission": _verified_cached_transmission(),
        "event_generated": True,
        "event_persisted": False,
        "event_id": "evt_retry_source",
    }
    source_url = "https://news.example.com/event-source"
    with patch(_GET_CACHED_EVENT, new_callable=AsyncMock, return_value=cached_data):
        with patch(f"{_MODULE}._analyze_understanding", new_callable=AsyncMock) as mock_u:
            with patch(
                f"{_MODULE}.persist_event_report",
                new_callable=AsyncMock,
                return_value=True,
            ) as mock_persist:
                with patch(f"{_MODULE}.set_cached_event", new_callable=AsyncMock):
                    await run({
                        "messages": [HumanMessage(content="测试事件")],
                        "analysis_reports": {"event_source": source_url},
                    })  # type: ignore[arg-type]

    mock_u.assert_not_called()
    mock_persist.assert_called_once()
    event_meta = mock_persist.call_args.args[1]
    assert event_meta.get("source") == source_url
