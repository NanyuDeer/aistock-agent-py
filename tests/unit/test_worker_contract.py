"""3 worker 契约测试 — stock/sector/hot_burst 输入对齐 + 副作用守卫

锁定 WorkerHandle A 契约（Task 2 直调 worker.run 消费）：
- 消费字段：stock(symbol) / sector(tag_code) / hot_burst(trigger_source)
- 副作用守卫：user_chat 下 hot_burst 不写 DB、不写本地报告缓存；
  scheduler 下允许（scheduler 守卫语义）。
- 顶层 try-catch 降级不抛（既有约束回归）。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from aistock_agent.agents.workers.hot_burst import run as run_hot_burst
from aistock_agent.agents.workers.sector import run as run_sector
from aistock_agent.agents.workers.stock import run as run_stock
from aistock_agent.prompts.workers.sector import SECTOR_ANALYST_PROMPT
from aistock_agent.services.data_client import HotBurstReadResult

_STOCK_CREATE = "aistock_agent.agents.workers.stock.create_react_agent"
_STOCK_LLM = "aistock_agent.agents.workers.stock.get_deep_think"
_SECTOR_CREATE = "aistock_agent.agents.workers.sector.create_react_agent"
_SECTOR_LLM = "aistock_agent.agents.workers.sector.get_deep_think"
_HOT_BURST_CREATE = "aistock_agent.agents.workers.hot_burst.create_react_agent"
_HOT_BURST_LLM = "aistock_agent.agents.workers.hot_burst.get_deep_think"
_HOT_BURST_NODE_API = "aistock_agent.agents.workers.hot_burst.node_api"
# set_report 在 run 内是延迟 import，patch 模块属性即可生效
_REPORT_CACHE_SET_REPORT = "aistock_agent.services.report_cache.set_report"

_SOURCE_DATA = {
    "total": 1,
    "records": [{"symbol": "300308", "stock_name": "中际旭创"}],
}
_VALID_BRIEF = (
    "今日机构调研热门方向集中在算力基础设施与高端制造。中际旭创和汇川技术在近期调研关注、板块消息及市场反馈中表现较突出，"
    "热门程度相对靠前。当前判断更偏向中期产业逻辑，但短期交易情绪可能放大波动，持续性仍需观察后续订单、行业景气和资金承接。"
    "主要风险包括热点快速降温、信息兑现不及预期及市场整体回撤。以上内容仅供参考，不构成投资建议。"
)
_VALID_RESPONSE = json.dumps(
    {
        "display_report": {
            "summary": "算力与高端制造关注度较高",
            "details": "重点分析热门程度、板块逻辑、持续性和风险。",
            "stocks": ["300308", "300124"],
            "risks": ["热点降温", "信息兑现不及预期"],
        },
        "podcast_brief": _VALID_BRIEF,
        "schema_version": "2.0",
    },
    ensure_ascii=False,
)


def _make_mock_agent(messages: list) -> MagicMock:
    """构造 mock react agent：ainvoke 返回 {"messages": messages}。"""
    mock_agent = MagicMock()
    mock_agent.ainvoke = AsyncMock(return_value={"messages": messages})
    return mock_agent


# ── hot_burst 副作用守卫（D7）─────────────────────────────────────


@pytest.mark.asyncio
async def test_hot_burst_user_chat_suppresses_db_and_cache_side_effects():
    """user_chat 触发：DB 落库与本地报告缓存均被抑制。"""
    with (
        patch(_HOT_BURST_NODE_API) as mock_api,
        patch(
            _HOT_BURST_CREATE,
            return_value=_make_mock_agent([AIMessage(content=_VALID_RESPONSE)]),
        ),
        patch(_HOT_BURST_LLM, return_value=MagicMock()),
        patch(_REPORT_CACHE_SET_REPORT) as mock_set_report,
    ):
        mock_api.get_hot_burst_data = AsyncMock(
            return_value=HotBurstReadResult("available", _SOURCE_DATA)
        )
        mock_api.save_analysis_report = AsyncMock()
        result = await run_hot_burst(
            {
                "trigger_source": "user_chat",
                "messages": [HumanMessage(content="分析今天的机构调研热门股")],
            }
        )

    mock_api.save_analysis_report.assert_not_awaited()
    mock_set_report.assert_not_called()
    assert result["final_response"]


@pytest.mark.asyncio
async def test_hot_burst_scheduler_allows_db_and_cache_side_effects():
    """scheduler 触发：DB 落库与本地报告缓存均允许（scheduler 守卫语义）。"""
    with (
        patch(_HOT_BURST_NODE_API) as mock_api,
        patch(
            _HOT_BURST_CREATE,
            return_value=_make_mock_agent([AIMessage(content=_VALID_RESPONSE)]),
        ),
        patch(_HOT_BURST_LLM, return_value=MagicMock()),
        patch(_REPORT_CACHE_SET_REPORT) as mock_set_report,
    ):
        mock_api.get_hot_burst_data = AsyncMock(
            return_value=HotBurstReadResult("available", _SOURCE_DATA)
        )
        mock_api.save_analysis_report = AsyncMock(return_value={"id": 1})
        await run_hot_burst(
            {
                "trigger_source": "scheduler",
                "report_date": "2026-07-15",
                "messages": [HumanMessage(content="分析今天的机构调研热门股")],
            }
        )

    mock_api.save_analysis_report.assert_awaited_once()
    mock_set_report.assert_called_once()
    call = mock_set_report.call_args
    assert call.args[0] == "hot_burst"
    assert call.args[1] == "2026-07-15"


# ── sector tag_code 输入对齐（D22/D24）────────────────────────────


@pytest.mark.asyncio
async def test_sector_run_with_tag_code_injects_bk_code_into_system_message():
    """tag_code 传入时，SystemMessage 内容注入 BK 码且保留原 prompt。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent.ainvoke = fake_ainvoke

    with (
        patch(_SECTOR_LLM, return_value=MagicMock()),
        patch(_SECTOR_CREATE, return_value=mock_agent),
    ):
        await run_sector(
            {"tag_code": "BK0438", "messages": [HumanMessage(content="分析白酒板块")]}
        )

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert "BK0438" in messages[0].content
    assert SECTOR_ANALYST_PROMPT in messages[0].content


@pytest.mark.asyncio
async def test_sector_run_without_tag_code_keeps_system_message_unchanged():
    """无 tag_code 时行为不变：SystemMessage 内容与 SECTOR_ANALYST_PROMPT 完全一致。"""
    captured: dict = {}
    mock_agent = MagicMock()

    async def fake_ainvoke(inp, **kw):
        captured.update(inp)
        return {"messages": [AIMessage(content="done")]}

    mock_agent.ainvoke = fake_ainvoke

    with (
        patch(_SECTOR_LLM, return_value=MagicMock()),
        patch(_SECTOR_CREATE, return_value=mock_agent),
    ):
        await run_sector({"messages": [HumanMessage(content="分析白酒板块")]})

    messages = captured["messages"]
    assert isinstance(messages[0], SystemMessage)
    assert messages[0].content == SECTOR_ANALYST_PROMPT


# ── stock symbol 输入校验 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_stock_run_missing_symbol_returns_hint_without_llm():
    """缺 symbol：返回提示文本且不触达 LLM/agent 创建。"""
    with (
        patch(_STOCK_LLM) as mock_llm,
        patch(_STOCK_CREATE) as mock_create,
    ):
        result = await run_stock({"messages": [HumanMessage(content="分析一下")]})

    assert "请提供股票代码" in result["final_response"]
    mock_llm.assert_not_called()
    mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_stock_run_with_symbol_reaches_llm():
    """有 symbol：正常走 ReAct 并返回 final_response。"""
    mock_agent = _make_mock_agent([AIMessage(content="贵州茅台分析完成")])
    with (
        patch(_STOCK_LLM, return_value=MagicMock()) as mock_llm,
        patch(_STOCK_CREATE, return_value=mock_agent),
    ):
        result = await run_stock(
            {"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]}
        )

    mock_llm.assert_called_once()
    assert result == {"final_response": "贵州茅台分析完成"}


# ── 顶层 try-catch 降级不抛（既有约束回归）────────────────────────


@pytest.mark.asyncio
async def test_stock_run_degradation_on_exception():
    """stock：LLM 初始化异常时返回降级文本，不抛异常。"""
    with patch(_STOCK_LLM, side_effect=RuntimeError("LLM down")):
        result = await run_stock(
            {"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]}
        )

    assert result["final_response"] == "个股分析暂时不可用，请稍后重试"


@pytest.mark.asyncio
async def test_sector_run_degradation_on_exception():
    """sector：LLM 初始化异常时返回降级文本，不抛异常。"""
    with patch(_SECTOR_LLM, side_effect=RuntimeError("LLM down")):
        result = await run_sector({"messages": [HumanMessage(content="分析白酒板块")]})

    assert result["final_response"] == "板块分析暂时不可用，请稍后重试"


@pytest.mark.asyncio
async def test_hot_burst_run_degradation_on_exception():
    """hot_burst：LLM 初始化异常时返回降级文本，不抛异常。"""
    with patch(_HOT_BURST_NODE_API) as mock_api:
        mock_api.get_hot_burst_data = AsyncMock(
            return_value=HotBurstReadResult("available", _SOURCE_DATA)
        )
        with patch(_HOT_BURST_LLM, side_effect=RuntimeError("LLM down")):
            result = await run_hot_burst(
                {
                    "trigger_source": "user_chat",
                    "messages": [HumanMessage(content="分析今天的机构调研热门股")],
                }
            )

    assert result["final_response"] == "机构调研热门股分析暂时不可用，请稍后重试"
