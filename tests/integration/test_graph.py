"""graph/builder.compile_graph 集成测试 — Phase 4 验收关键证据

验证「完整消息流程：输入 → 路由 → agent → 回复」端到端跑通。
覆盖 5 条 intent 路径 + 未知 intent 兜底 + 完整流程（含 state 传递）。

关键模式（来自 Task 5 的 test_memory.py）：
    LangGraph 在 ``build_graph()`` 时通过 ``graph.add_node("supervisor", supervisor.run)``
    绑定节点函数引用。因此 patch 必须在 ``compile_graph()`` 之前生效——
    在 ``with patch(...)`` 块内调用 ``compile_graph()``，``build_graph()`` 会读取
    patch 后的模块属性，拿到 mock 函数。

mock 节点函数签名：
    supervisor.run(state) → {"intent": "...", "symbol": "...", "tag_code": "..."}
    agent.run(state)      → {"final_response": "..."}
"""

from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.graph.builder import compile_graph

# 节点函数 patch 路径（与 src/aistock_agent/graph/builder.py 导入路径一致）
NODE_PATHS: dict[str, str] = {
    "supervisor": "aistock_agent.agents.supervisor.node.run",
    "morning": "aistock_agent.agents.workers.morning.run",
    "stock": "aistock_agent.agents.workers.stock.run",
    "sector": "aistock_agent.agents.workers.sector.run",
    "event": "aistock_agent.agents.workers.event.run",
    "general": "aistock_agent.agents.general.node.run",
}

# 各 agent mock 的默认 final_response（互不相同，便于断言路由命中目标）
_DEFAULT_AGENT_RESPONSES: dict[str, str] = {
    "morning": "morning briefing",
    "stock": "stock analysis",
    "sector": "sector analysis",
    "event": "event analysis",
    "general": "general response",
}


# ── 公共 fixture / 辅助 ───────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_checkpointer_singleton():
    """每个测试前重置 checkpointer 单例，避免跨测试 checkpoint 数据残留。

    与 tests/integration/test_memory.py 保持一致；不同 thread_id 已隔离数据，
    这里再重置单例仅为保持测试卫生。
    """
    from aistock_agent.memory import checkpointer as cp_module

    cp_module._checkpointer = None
    yield
    cp_module._checkpointer = None


def _build_initial_state(user_message: str = "分析一下") -> dict[str, object]:
    """构造合法的 AgentState 初始值。"""
    return {
        "messages": [{"role": "user", "content": user_message}],
        "session_id": "test-graph",
        "user_id": None,
        "favorites": [],
        "intent": None,
        "symbol": None,
        "tag_code": None,
        "analysis_reports": {},
        "final_response": None,
    }


@contextmanager
def _patch_nodes(supervisor_return: dict[str, object]):
    """Patch supervisor + 5 个 agent 的 run 函数，yield {name: AsyncMock}。

    所有 agent 均被 patch，确保路由错误时不会触达真实 LLM/工具调用，
    保证测试确定性与隔离性。每个 mock 返回互不相同的 final_response。
    """
    mocks: dict[str, AsyncMock] = {
        "supervisor": AsyncMock(return_value=supervisor_return),
    }
    for name, resp in _DEFAULT_AGENT_RESPONSES.items():
        mocks[name] = AsyncMock(return_value={"final_response": resp})

    with ExitStack() as stack:
        for name in ("supervisor", "morning", "stock", "sector", "event", "general"):
            stack.enter_context(patch(NODE_PATHS[name], new=mocks[name]))
        yield mocks


async def _invoke(intent_supervisor_return: dict[str, object], thread_id: str) -> tuple[dict[str, object], dict[str, AsyncMock]]:
    """在 patch 块内 compile_graph + ainvoke，返回 (result, mocks)。"""
    with _patch_nodes(intent_supervisor_return) as mocks:
        graph = compile_graph()
        result = await graph.ainvoke(
            _build_initial_state(),
            config={"configurable": {"thread_id": thread_id}},
        )
    return result, mocks


# ── 5 条 intent 路径 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_routes_morning():
    """supervisor 返回 intent=morning → 路由到 morning_agent，final_response 命中。"""
    result, mocks = await _invoke({"intent": "morning"}, "test_graph_routes_morning")

    mocks["morning"].assert_awaited_once()
    mocks["stock"].assert_not_awaited()
    mocks["sector"].assert_not_awaited()
    mocks["event"].assert_not_awaited()
    mocks["general"].assert_not_awaited()
    assert result.get("final_response") == "morning briefing"


@pytest.mark.asyncio
async def test_graph_routes_stock():
    """supervisor 返回 intent=stock → 路由到 stock_analyst。"""
    result, mocks = await _invoke({"intent": "stock"}, "test_graph_routes_stock")

    mocks["stock"].assert_awaited_once()
    mocks["morning"].assert_not_awaited()
    mocks["sector"].assert_not_awaited()
    mocks["event"].assert_not_awaited()
    mocks["general"].assert_not_awaited()
    assert result.get("final_response") == "stock analysis"


@pytest.mark.asyncio
async def test_graph_routes_sector():
    """supervisor 返回 intent=sector → 路由到 sector_analyst。"""
    result, mocks = await _invoke({"intent": "sector"}, "test_graph_routes_sector")

    mocks["sector"].assert_awaited_once()
    mocks["morning"].assert_not_awaited()
    mocks["stock"].assert_not_awaited()
    mocks["event"].assert_not_awaited()
    mocks["general"].assert_not_awaited()
    assert result.get("final_response") == "sector analysis"


@pytest.mark.asyncio
async def test_graph_routes_event():
    """supervisor 返回 intent=event → 路由到 event_analyst。"""
    result, mocks = await _invoke({"intent": "event"}, "test_graph_routes_event")

    mocks["event"].assert_awaited_once()
    mocks["morning"].assert_not_awaited()
    mocks["stock"].assert_not_awaited()
    mocks["sector"].assert_not_awaited()
    mocks["general"].assert_not_awaited()
    assert result.get("final_response") == "event analysis"


@pytest.mark.asyncio
async def test_graph_routes_general():
    """supervisor 返回 intent=general → 路由到 general_agent。"""
    result, mocks = await _invoke({"intent": "general"}, "test_graph_routes_general")

    mocks["general"].assert_awaited_once()
    mocks["morning"].assert_not_awaited()
    mocks["stock"].assert_not_awaited()
    mocks["sector"].assert_not_awaited()
    mocks["event"].assert_not_awaited()
    assert result.get("final_response") == "general response"


# ── 未知 intent 兜底 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_routes_unknown_intent_to_general():
    """supervisor 返回未知 intent → route_by_intent 兜底到 general_agent。"""
    result, mocks = await _invoke(
        {"intent": "unknown_intent"}, "test_graph_routes_unknown_intent_to_general"
    )

    # route_by_intent 对未注册 intent fallback 到 general
    mocks["general"].assert_awaited_once()
    mocks["morning"].assert_not_awaited()
    mocks["stock"].assert_not_awaited()
    mocks["sector"].assert_not_awaited()
    mocks["event"].assert_not_awaited()
    assert result.get("final_response") == "general response"


# ── 完整流程（含 state 传递）──────────────────────────────────────


@pytest.mark.asyncio
async def test_graph_full_flow_with_mock_agents():
    """完整流程：supervisor 写入 intent+symbol → stock_analyst 收到 state → final_response。

    验证：
    - supervisor.run 被调用（输入端）
    - stock.run 被调用且收到的 state 含 supervisor 写入的 symbol/intent（state 传递）
    - result.final_response 非空且等于 mock 返回值（输出端）
    """
    supervisor_return = {"intent": "stock", "symbol": "600519"}
    with _patch_nodes(supervisor_return) as mocks:
        graph = compile_graph()
        result = await graph.ainvoke(
            _build_initial_state("分析 600519"),
            config={"configurable": {"thread_id": "test_graph_full_flow"}},
        )

    mocks["supervisor"].assert_awaited_once()
    mocks["stock"].assert_awaited_once()

    # state 传递：stock.run 收到的 state 应含 supervisor 写入的路由信息
    stock_call_args = mocks["stock"].await_args
    assert stock_call_args is not None
    stock_call_state = stock_call_args.args[0]
    assert stock_call_state.get("symbol") == "600519"
    assert stock_call_state.get("intent") == "stock"

    final = result.get("final_response")
    assert final  # 非空
    assert final == "stock analysis"
