"""agent run() 异常降级集成测试 — Task 9

验证 6 个 agent 的 run() 顶层 try-catch（落实 E2 + AGENTS.md "Tool 失败返回
降级文本，不抛异常中断图执行"）：
- LLM/Graph 框架异常（get_deep_think/get_quick_think 失败）被捕获
- 返回符合 AGENTS.md 规范的降级文本（标注"暂不可用"，不猜测数据）
- graph.ainvoke 在 agent 返回降级文本时正常返回（不中断）

mock 策略（与 tests/integration/test_{stock,general,supervisor}_agent.py 一致）：
- patch 各 agent 模块内的 get_deep_think / get_quick_think 抛 Exception
- morning 额外 patch _get_cached_briefing 返回 None（避免缓存短路 + 避免真实 Redis）
- stock 的 state 含 symbol="600519"（避免 if not symbol 早返回）
"""

from contextlib import ExitStack
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.agents.general.node import run as general_run
from aistock_agent.agents.supervisor.node import run as supervisor_run
from aistock_agent.agents.workers.event import run as event_run
from aistock_agent.agents.workers.morning import run as morning_run
from aistock_agent.agents.workers.sector import run as sector_run
from aistock_agent.agents.workers.stock import run as stock_run
from aistock_agent.graph.builder import compile_graph

# patch 路径（与各 agent 测试文件一致）
_GET_QUICK_SUPERVISOR = "aistock_agent.agents.supervisor.node.get_quick_think"
_GET_CACHED_MORNING = "aistock_agent.agents.workers.morning._get_cached_briefing"
_GET_DEEP_MORNING = "aistock_agent.agents.workers.morning.get_deep_think"
_GET_DEEP_STOCK = "aistock_agent.agents.workers.stock.get_deep_think"
_GET_DEEP_SECTOR = "aistock_agent.agents.workers.sector.get_deep_think"
_GET_DEEP_EVENT = "aistock_agent.agents.workers.event.get_deep_think"
_GET_QUICK_GENERAL = "aistock_agent.agents.general.node.get_quick_think"

# 降级文本（与各 agent run() except 块一致）
_MORNING_FALLBACK = "晨报生成暂时不可用，请稍后重试"
_STOCK_FALLBACK = "个股分析暂时不可用，请稍后重试"
_SECTOR_FALLBACK = "板块分析暂时不可用，请稍后重试"
_EVENT_FALLBACK = "事件分析暂时不可用，请稍后重试"
_GENERAL_FALLBACK = "抱歉，我暂时无法处理您的请求，请稍后重试"

# graph 节点 patch 路径（与 tests/integration/test_graph.py 一致）
NODE_PATHS: dict[str, str] = {
    "supervisor": "aistock_agent.agents.supervisor.node.run",
    "morning": "aistock_agent.agents.workers.morning.run",
    "stock": "aistock_agent.agents.workers.stock.run",
    "sector": "aistock_agent.agents.workers.sector.run",
    "event": "aistock_agent.agents.workers.event.run",
    "general": "aistock_agent.agents.general.node.run",
}


# ── 公共 fixture ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_checkpointer_singleton():
    """每个测试前重置 checkpointer 单例，避免跨测试 checkpoint 数据残留。

    与 tests/integration/test_graph.py 保持一致。
    """
    from aistock_agent.memory import checkpointer as cp_module

    cp_module._checkpointer = None
    yield
    cp_module._checkpointer = None


# ── 6 个 agent run() 异常降级 ────────────────────────────────────


@pytest.mark.asyncio
async def test_supervisor_fallback_on_llm_failure():
    """supervisor.run: get_quick_think 抛异常 → 降级返回 intent='general'。"""
    state = {"messages": [HumanMessage(content="分析一下")]}
    with patch(_GET_QUICK_SUPERVISOR, side_effect=Exception("LLM boom")):
        result = await supervisor_run(state)
    assert result == {"intent": "general"}


@pytest.mark.asyncio
async def test_morning_fallback_on_llm_failure():
    """morning.run: get_deep_think 抛异常 → 降级返回晨报不可用文本。

    额外 patch _get_cached_briefing 返回 None，避免缓存命中短路。
    """
    with patch(_GET_CACHED_MORNING, AsyncMock(return_value=None)):
        with patch(_GET_DEEP_MORNING, side_effect=Exception("LLM boom")):
            result = await morning_run({})
    assert result == {"final_response": _MORNING_FALLBACK}


@pytest.mark.asyncio
async def test_stock_fallback_on_llm_failure():
    """stock.run: get_deep_think 抛异常 → 降级返回个股分析不可用文本。

    state 含 symbol='600519'，避免 if not symbol 早返回。
    """
    state = {"symbol": "600519", "messages": [HumanMessage(content="分析 600519")]}
    with patch(_GET_DEEP_STOCK, side_effect=Exception("LLM boom")):
        result = await stock_run(state)
    assert result == {"final_response": _STOCK_FALLBACK}


@pytest.mark.asyncio
async def test_sector_fallback_on_llm_failure():
    """sector.run: get_deep_think 抛异常 → 降级返回板块分析不可用文本。"""
    state = {"messages": [HumanMessage(content="分析白酒板块")]}
    with patch(_GET_DEEP_SECTOR, side_effect=Exception("LLM boom")):
        result = await sector_run(state)
    assert result == {"final_response": _SECTOR_FALLBACK}


@pytest.mark.asyncio
async def test_event_fallback_on_llm_failure():
    """event.run: get_deep_think 抛异常 → 降级返回事件分析不可用文本。"""
    state = {"messages": [HumanMessage(content="分析美联储加息")]}
    with patch(_GET_DEEP_EVENT, side_effect=Exception("LLM boom")):
        result = await event_run(state)
    assert result == {"final_response": _EVENT_FALLBACK}


@pytest.mark.asyncio
async def test_general_fallback_on_llm_failure():
    """general.run: get_quick_think 抛异常 → 降级返回通用兜底文本。"""
    state = {"messages": [HumanMessage(content="你好")]}
    with patch(_GET_QUICK_GENERAL, side_effect=Exception("LLM boom")):
        result = await general_run(state)
    assert result == {"final_response": _GENERAL_FALLBACK}


# ── graph 不中断测试 ─────────────────────────────────────────────


def _build_initial_state(user_message: str = "分析一下 600519") -> dict[str, Any]:
    """构造合法的 AgentState 初始值（与 test_graph.py 一致）。"""
    return {
        "messages": [{"role": "user", "content": user_message}],
        "session_id": "test-fallback",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }


@pytest.mark.asyncio
async def test_graph_continues_when_agent_returns_fallback():
    """agent 返回降级文本时 graph 正常返回（不中断）。

    语义：agent run() 内部 try-catch 把异常转为降级文本，graph 收到降级文本
    正常返回。mock stock.run 返回降级文本（模拟 try-catch 已生效），
    验证 graph.ainvoke 返回该文本而非抛异常。

    与 test_graph.py 一致：patch 必须在 compile_graph() 之前生效
    （LangGraph 在 build_graph 时绑定节点函数引用）。
    """
    supervisor_return = {"intent": "stock", "symbol": "600519"}
    stock_return = {"final_response": _STOCK_FALLBACK}

    with ExitStack() as stack:
        stack.enter_context(
            patch(NODE_PATHS["supervisor"], new=AsyncMock(return_value=supervisor_return))
        )
        stack.enter_context(
            patch(NODE_PATHS["stock"], new=AsyncMock(return_value=stock_return))
        )
        # 其他 agent 也 patch，避免误触达真实 LLM
        for name in ("morning", "sector", "event", "general"):
            stack.enter_context(
                patch(
                    NODE_PATHS[name],
                    new=AsyncMock(return_value={"final_response": f"{name} response"}),
                )
            )

        graph = compile_graph()
        result = await graph.ainvoke(
            _build_initial_state(),
            config={"configurable": {"thread_id": "test_graph_fallback"}},
        )

    assert result.get("final_response") == _STOCK_FALLBACK
