"""alert_agent run() 集成测试 — 异动提醒多维分析

mock create_react_agent + asyncio.gather，验证：
- 3 个子 Agent 工具集绑定（alert_news/alert_risk/alert_graph）
- 子 Agent 异常降级不中断整体流程
- Master Agent 汇聚子结果
- symbol 缺失时返回提示文本
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from aistock_agent.agents.workers.alert import run

_CREATE_REACT = "aistock_agent.agents.workers.alert.create_react_agent"
_GET_DEEP = "aistock_agent.agents.workers.alert.get_deep_think"
_GET_QUICK = "aistock_agent.agents.workers.alert.get_quick_think"


def _make_mock_agent(*responses: str) -> MagicMock:
    """构造 mock react agent，每次 ainvoke 返回下一个 response。"""
    calls = list(responses)
    mock_agent = MagicMock()

    async def fake_ainvoke(_input, **kw):
        msg = calls.pop(0) if calls else "done"
        return {"messages": [AIMessage(content=msg)]}

    mock_agent.ainvoke = fake_ainvoke
    return mock_agent


def _make_split_mocks(preview_json: str, detail_json: str):
    """构造速览/详情两个 mock agent（按 create_react_agent 调用顺序返回）。"""
    return [_make_mock_agent(preview_json), _make_mock_agent(detail_json)]


_PREVIEW_JSON = '{"summary":"异动结论","impact":"利好","keywords":["涨价"]}'
_DETAIL_JSON = (
    '{"details":"## 详情","stocks":["600519"],"risks":["风险A"],'
    '"podcast_brief":"异动摘要"}'
)


# ═══════════════════════════════════════════════════════════════════════════════
# 子 Agent 工具注册验证
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_alert_news_tools_registered():
    """alert_news 类注册了 search_cls_news + tavily_finance_search 两个工具。"""
    from aistock_agent.tools.registry import get_tools
    tools = get_tools("alert_news")
    names = {t.name for t in tools}
    assert names == {"search_cls_news", "tavily_finance_search"}


@pytest.mark.asyncio
async def test_alert_risk_tools_registered():
    """alert_risk 类注册了 get_quote + get_capital_flow 两个工具。"""
    from aistock_agent.tools.registry import get_tools
    tools = get_tools("alert_risk")
    names = {t.name for t in tools}
    assert names == {"get_quote", "get_capital_flow"}


@pytest.mark.asyncio
async def test_alert_graph_tools_registered():
    """alert_graph 类注册了 get_concepts + get_graph_by_concept 两个工具。"""
    from aistock_agent.tools.registry import get_tools
    tools = get_tools("alert_graph")
    names = {t.name for t in tools}
    assert names == {"get_concepts", "get_graph_by_concept"}


# ═══════════════════════════════════════════════════════════════════════════════
# Agent 行为测试
# ═══════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_symbol_missing_returns_hint():
    """symbol 缺失时返回提示文本，不调用 LLM。"""
    mock_llm = MagicMock()
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock()
    with patch(_GET_DEEP, return_value=mock_llm) as mock_llm_factory:
        with patch(_CREATE_REACT, return_value=mock_agent) as mock_create:
            result = await run({"messages": [HumanMessage(content="有什么异动")]})

    assert result == {"final_response": "请提供股票代码，例如：分析一下 600519 的异动"}
    mock_llm_factory.assert_not_called()
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_master_synthesizes_sub_agent_results():
    """3 个子 Agent 各自返回结果，Master 拿到全部结果后生成最终报告。"""
    agents = _make_split_mocks(_PREVIEW_JSON, _DETAIL_JSON)
    with (
        patch(_GET_DEEP, return_value=MagicMock()),
        patch(_GET_QUICK, return_value=MagicMock()),
        patch(_CREATE_REACT, side_effect=agents),
        patch("aistock_agent.agents.workers.alert._run_sub_agent", autospec=True) as mock_sub,
    ):
        mock_sub.side_effect = [
            "资讯情报分析结果",
            "盘口风控分析结果",
            "图谱发散分析结果",
        ]

        result = await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519 异动")]})

    # 3 个子 Agent 各被调用一次
    assert mock_sub.call_count == 3
    merged = json.loads(result["final_response"])
    assert merged["display_report"] == {
        "summary": "异动结论",
        "impact": "利好",
        "keywords": ["涨价"],
        "details": "## 详情",
        "stocks": ["600519"],
        "risks": ["风险A"],
    }
    assert "analysis_reports" in result


@pytest.mark.asyncio
async def test_scheduler_persists_alert_with_symbol_bound_real_structure():
    """调度产生的 alert 以股票代码作为实体键，并写入可校验的 content 结构。"""
    save_report = AsyncMock()
    with (
        patch(_GET_DEEP, return_value=MagicMock()),
        patch(_GET_QUICK, return_value=MagicMock()),
        patch(_CREATE_REACT, side_effect=_make_split_mocks(_PREVIEW_JSON, _DETAIL_JSON)),
        patch("aistock_agent.agents.workers.alert._run_sub_agent", autospec=True, return_value="子报告"),
        patch("aistock_agent.agents.workers.alert.node_api.save_analysis_report", save_report),
    ):
        await run({
            "symbol": "600519",
            "messages": [HumanMessage(content="分析 600519 异动")],
            "trigger_source": "scheduler",
            "report_date": "2026-07-10",
        })

    assert save_report.await_args.kwargs == {
        "report_type": "alert",
        "report_date": "2026-07-10",
        "content": {
            "symbol": "600519",
            "display_report": {
                "summary": "异动结论",
                "impact": "利好",
                "keywords": ["涨价"],
                "details": "## 详情",
                "stocks": ["600519"],
                "risks": ["风险A"],
            },
            "podcast_brief": "异动摘要",
        },
        "user_id": "600519",
        "data_source": "alert_agent",
    }


@pytest.mark.asyncio
async def test_sub_agent_failure_not_crash():
    """某个子 Agent 失败时返回降级文本，不中断整体流程。"""
    agents = _make_split_mocks(_PREVIEW_JSON, _DETAIL_JSON)
    with (
        patch(_GET_DEEP, return_value=MagicMock()),
        patch(_GET_QUICK, return_value=MagicMock()),
        patch(_CREATE_REACT, side_effect=agents),
        patch("aistock_agent.agents.workers.alert._run_sub_agent", autospec=True) as mock_sub,
    ):
        # 模拟盘口风控子 Agent 降级
        mock_sub.side_effect = [
            "资讯情报分析结果",
            "[盘口风控] 分析暂时不可用",
            "图谱发散分析结果",
        ]

        result = await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519 异动")]})

    # 子 Agent 降级不影响速览/详情双调用输出合并报告
    merged = json.loads(result["final_response"])
    assert merged["display_report"]["summary"] == "异动结论"
    assert merged["podcast_brief"] == "异动摘要"


@pytest.mark.asyncio
async def test_run_uses_both_llm_types():
    """alert_agent run() 中 Master 使用 get_deep_think，子 Agent 按分工分配模型。"""
    agents = _make_split_mocks(_PREVIEW_JSON, _DETAIL_JSON)
    with (
        patch(_GET_DEEP, return_value=MagicMock()) as mock_deep,
        patch(_GET_QUICK, return_value=MagicMock()) as mock_quick,
        patch(_CREATE_REACT, side_effect=agents),
        patch("aistock_agent.agents.workers.alert._run_sub_agent", autospec=True) as mock_sub,
    ):
        mock_sub.return_value = "子Agent结果"

        await run({"symbol": "600519", "messages": [HumanMessage(content="分析 600519 异动")]})

    assert mock_deep.call_count >= 1  # 详情 Master 用了 deep_think
    # 验证 3 个子 Agent 调用：资讯情报(quick) + 盘口风控(deep) + 图谱发散(quick)
    assert mock_sub.call_count == 3
    actual_calls = [c.kwargs for c in mock_sub.call_args_list]
    assert any(c["name"] == "资讯情报" and c["model_type"] == "quick" for c in actual_calls)
    assert any(c["name"] == "盘口风控" and c["model_type"] == "deep" for c in actual_calls)
    assert any(c["name"] == "图谱发散" and c["model_type"] == "quick" for c in actual_calls)


@pytest.mark.asyncio
async def test_stock_trace_saves_with_correct_contract():
    """stock_trace trigger_source 以 symbol 为 user_id，写入 stock_trace.v1 payload。"""
    save_report = AsyncMock(return_value={"id": 123})
    with (
        patch(_GET_DEEP, return_value=MagicMock()),
        patch(_GET_QUICK, return_value=MagicMock()),
        patch(_CREATE_REACT, side_effect=_make_split_mocks(_PREVIEW_JSON, _DETAIL_JSON)),
        patch("aistock_agent.agents.workers.alert._run_sub_agent", autospec=True, return_value="子报告"),
        patch("aistock_agent.agents.workers.alert.node_api.save_analysis_report", save_report),
    ):
        result = await run({
            "symbol": "600519",
            "messages": [HumanMessage(content="分析 600519 异动")],
            "trigger_source": "stock_trace",
            "report_date": "2026-07-10",
            "trace_id": "trace-abc-123",
        })

    # 保存语义：alert + report_date + user_id=symbol + stock_trace.v1
    assert save_report.await_args.kwargs == {
        "report_type": "alert",
        "report_date": "2026-07-10",
        "user_id": "600519",
        "data_source": "stock_trace",
        "content": {
            "schema_version": "stock_trace.v1",
            "trace_id": "trace-abc-123",
            "symbol": "600519",
            "display_report": {
                "summary": "异动结论",
                "impact": "利好",
                "keywords": ["涨价"],
                "details": "## 详情",
                "stocks": ["600519"],
                "risks": ["风险A"],
            },
            "podcast_brief": "异动摘要",
        },
    }
    # 返回标记供 route 判断
    assert result.get("trace_persisted") is True
    assert result.get("report_id") == 123
    assert result.get("trace_id") == "trace-abc-123"


@patch("aistock_agent.services.report_cache.set_report")
@pytest.mark.asyncio
async def test_user_trigger_does_not_save_report(
    mock_set_report: MagicMock,
):
    """trigger_source=user 时只缓存，不调用 save_analysis_report。"""
    save_report = AsyncMock()
    with (
        patch(_GET_DEEP, return_value=MagicMock()),
        patch(_GET_QUICK, return_value=MagicMock()),
        patch(_CREATE_REACT, side_effect=_make_split_mocks(_PREVIEW_JSON, _DETAIL_JSON)),
        patch("aistock_agent.agents.workers.alert._run_sub_agent", autospec=True, return_value="子报告"),
        patch("aistock_agent.agents.workers.alert.node_api.save_analysis_report", save_report),
    ):
        result = await run({
            "symbol": "600519",
            "messages": [HumanMessage(content="分析 600519 异动")],
            "trigger_source": "user",
            "report_date": "2026-07-10",
        })

    # user 不写入数据库
    save_report.assert_not_called()
    # user 写 report_cache（供列表查询）
    assert mock_set_report.called
    assert result.get("final_response") is not None


@pytest.mark.asyncio
async def test_master_split_merges_preview_and_detail():
    """速览与详情合并成 6 字段 display_report（职责不重叠）。"""
    agents = _make_split_mocks(_PREVIEW_JSON, _DETAIL_JSON)
    with (
        patch(_GET_DEEP, return_value=MagicMock()),
        patch(_GET_QUICK, return_value=MagicMock()),
        patch(_CREATE_REACT, side_effect=agents),
        patch("aistock_agent.agents.workers.alert._run_sub_agent", autospec=True, return_value="子报告"),
    ):
        result = await run({
            "symbol": "600519",
            "messages": [HumanMessage(content="分析 600519 异动")],
            "trigger_source": "user",
            "report_date": "2026-07-10",
        })

    merged = json.loads(result["final_response"])
    assert merged["display_report"] == {
        "summary": "异动结论",
        "impact": "利好",
        "keywords": ["涨价"],
        "details": "## 详情",
        "stocks": ["600519"],
        "risks": ["风险A"],
    }
    assert merged["podcast_brief"] == "异动摘要"


@pytest.mark.asyncio
async def test_run_tolerates_detail_failure_keeps_preview():
    """详情抛异常时 run() 用速览降级合并、不抛异常（合并前必修 A）。

    修复前 gather 无 return_exceptions，_run_master_detail 抛异常会整体上抛 →
    run() 落入 except 返回降级提示文本，速览已成功的结果被连带丢弃；
    本用例在未修实现下应因 final_response 非常规 JSON 而失败（RED）。
    """
    with (
        patch("aistock_agent.agents.workers.alert._run_sub_agent",
              autospec=True, return_value="子报告"),
        patch("aistock_agent.agents.workers.alert._run_master_preview",
              new=AsyncMock(return_value={
                  "summary": "异动结论", "impact": "利好", "keywords": ["涨价"],
              })),
        patch("aistock_agent.agents.workers.alert._run_master_detail",
              new=AsyncMock(side_effect=RuntimeError("detail boom"))),
        patch("aistock_agent.agents.workers.alert._cache_alert_result"),
    ):
        result = await run({
            "symbol": "600519",
            "messages": [HumanMessage(content="分析 600519 异动")],
        })

    merged = json.loads(result["final_response"])
    assert merged["display_report"] == {
        "summary": "异动结论",
        "impact": "利好",
        "keywords": ["涨价"],
        "details": "",
        "stocks": [],
        "risks": [],
    }
    assert merged["podcast_brief"] == ""
