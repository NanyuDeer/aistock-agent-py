"""escalate 节点单元测试 — WorkerHandle 协议 + deep 分支 worker 调度（Task 2）。

锁定契约：
- 意图 → worker 名映射（INTENT_TO_WORKER）与 worker 注册表（ESCALATION_MAP）
- AgentState 只填 worker 消费字段，trigger_source="user_chat" 固定（D7）
- sector 未命中 tag_code → fallback_to_skill（D24），不调 worker
- worker 异常 / 空 final_response → 降级文本，不抛（两层降级体系）
- deep_source 只写合法 worker 名
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.nodes.escalate import (
    ESCALATION_MAP,
    INTENT_TO_WORKER,
    escalate_node,
)
from aistock_agent.schemas.chat_contract import InsightGoal

_DEGRADED_TEXT = "深度分析暂时不可用，请稍后重试"


def _state(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "messages": [HumanMessage(content="分析 600519")],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "clarification": None,
        "complexity": "deep",
    }
    base.update(overrides)
    return base


def _goal(intent: str, **kwargs: object) -> InsightGoal:
    return InsightGoal(question=kwargs.pop("question", "分析 600519"), intent=intent, **kwargs)


def _make_worker(result: dict | None = None, exc: Exception | None = None) -> MagicMock:
    """构造 WorkerHandle 形状的 mock：`run(state) -> Awaitable[dict]`（A 契约）。

    escalate 以 `await worker.run(agent_state)` 调用，因此 mock 必须暴露
    AsyncMock 属性的 run（直接放 AsyncMock 实例会被当成 run 方法本身）。
    """
    worker = MagicMock()
    worker.run = AsyncMock(return_value=result, side_effect=exc)
    return worker


def test_worker_handle_protocol_maps_cover_expected_intents():
    """升级意图 → worker 名映射与 ESCALATION_MAP 注册覆盖锁定（D6/D1）。"""
    assert set(INTENT_TO_WORKER) == {
        "stock_snapshot",
        "stock_news",
        "capital_flow",
        "sector_snapshot",
        "hot_burst",
    }
    assert set(ESCALATION_MAP) == {"stock", "sector", "hot_burst"}
    for worker in ESCALATION_MAP.values():
        assert callable(worker)


@pytest.mark.asyncio
async def test_escalate_stock_passes_symbol_and_user_chat():
    """stock 意图 → AgentState.symbol=600519、trigger_source=user_chat，final_response 回流。"""
    mock_worker = _make_worker(result={"final_response": "分析全文"})
    state = _state(
        goal=_goal("stock_snapshot", symbols=["600519"], question="深度分析一下 600519")
    )

    with patch.dict(ESCALATION_MAP, {"stock": mock_worker}):
        result = await escalate_node(state)

    args = mock_worker.run.await_args.args[0]
    assert args["symbol"] == "600519"
    assert args["tag_code"] is None
    assert args["trigger_source"] == "user_chat"
    assert len(args["messages"]) == 1
    assert result["deep_source"] == "stock"
    assert result["final_response"] == "分析全文"


@pytest.mark.asyncio
async def test_escalate_sector_resolves_tag_code():
    """sector 意图无 tag_codes → 中文名 resolve_tag_code → BK；命中调 worker。"""
    mock_worker = _make_worker(result={"final_response": "板块分析全文"})
    state = _state(
        goal=_goal("sector_snapshot", question="分析一下白酒板块"),
    )

    with (
        patch(
            "aistock_agent.graph.nodes.escalate.resolve_tag_code",
            return_value="BK0477",
        ) as mock_resolve,
        patch.dict(ESCALATION_MAP, {"sector": mock_worker}),
    ):
        result = await escalate_node(state)

    args = mock_worker.run.await_args.args[0]
    assert args["tag_code"] == "BK0477"
    assert args["symbol"] is None
    assert args["trigger_source"] == "user_chat"
    mock_resolve.assert_any_call("白酒")
    assert result["deep_source"] == "sector"
    assert result["final_response"] == "板块分析全文"


@pytest.mark.asyncio
async def test_escalate_sector_fallback_to_skill():
    """sector tag_code 未命中 → fallback_to_skill=True，不调 worker。"""
    mock_worker = _make_worker()
    state = _state(
        goal=_goal("sector_snapshot", question="分析一下白酒板块"),
    )

    with (
        patch("aistock_agent.graph.nodes.escalate.resolve_tag_code", return_value=None),
        patch.dict(ESCALATION_MAP, {"sector": mock_worker}),
    ):
        result = await escalate_node(state)

    assert result == {"fallback_to_skill": True}
    mock_worker.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalate_hot_burst_no_params():
    """hot_burst 意图 → 无 symbol/tag_code 直调 worker，trigger_source=user_chat。"""
    mock_worker = _make_worker(result={"final_response": "热门股分析全文"})
    state = _state(goal=_goal("hot_burst", question="分析今天的机构调研热门股"))

    with patch.dict(ESCALATION_MAP, {"hot_burst": mock_worker}):
        result = await escalate_node(state)

    args = mock_worker.run.await_args.args[0]
    assert args["symbol"] is None
    assert args["tag_code"] is None
    assert args["trigger_source"] == "user_chat"
    assert result["deep_source"] == "hot_burst"
    assert result["final_response"] == "热门股分析全文"


@pytest.mark.asyncio
async def test_escalate_worker_exception_returns_degraded():
    """worker.run 抛异常 → 降级文本，不抛、不中断；deep_source 仍写入。"""
    mock_worker = _make_worker(exc=RuntimeError("boom"))
    state = _state(goal=_goal("stock_snapshot", symbols=["600519"]))

    with patch.dict(ESCALATION_MAP, {"stock": mock_worker}):
        result = await escalate_node(state)

    assert result["final_response"] == _DEGRADED_TEXT
    assert result["deep_source"] == "stock"


@pytest.mark.asyncio
async def test_escalate_unknown_intent_falls_back():
    """ESCALATION_MAP 无此意图 → fallback_to_skill=True，不调 worker。"""
    mock_worker = _make_worker()
    state = _state(goal=_goal("report_lookup", question="查一下昨天的晨报"))

    with patch.dict(ESCALATION_MAP, {"stock": mock_worker}):
        result = await escalate_node(state)

    assert result == {"fallback_to_skill": True}
    mock_worker.run.assert_not_awaited()


@pytest.mark.asyncio
async def test_escalate_empty_final_response_degraded():
    """worker 返回空 final_response → 降级兜底文本。"""
    mock_worker = _make_worker(result={"final_response": ""})
    state = _state(goal=_goal("stock_snapshot", symbols=["600519"]))

    with patch.dict(ESCALATION_MAP, {"stock": mock_worker}):
        result = await escalate_node(state)

    assert result["final_response"] == _DEGRADED_TEXT
    assert result["deep_source"] == "stock"


@pytest.mark.asyncio
async def test_escalate_missing_goal_falls_back():
    """goal 缺失 → fallback_to_skill=True（不中断）。"""
    result = await escalate_node({"messages": [HumanMessage(content="hi")]})

    assert result == {"fallback_to_skill": True}
