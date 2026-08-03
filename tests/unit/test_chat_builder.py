"""CHAT 子图构建测试。

- 编译可用性（既有 2 例）
- P1 拓扑条件路由（Task 2）：deep 无短路 → escalate；light/短路 → skill_executor；
  escalate fallback_to_skill → skill_executor。
"""

from unittest.mock import patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.graph.chat_builder import (
    compile_chat_graph,
    route_after_escalate,
    route_after_router,
)
from aistock_agent.schemas.chat_contract import InsightGoal


def test_compile_chat_graph_returns_runnable():
    """图可正常编译，返回可调用对象。"""
    graph = compile_chat_graph(checkpointer=None)
    assert graph is not None
    assert hasattr(graph, "ainvoke")
    assert hasattr(graph, "astream_events")


def test_compile_chat_graph_idempotent():
    """多次编译返回独立实例。"""
    g1 = compile_chat_graph(checkpointer=None)
    g2 = compile_chat_graph(checkpointer=None)
    assert g1 is not g2


# ── 路由函数单元语义（纯函数，无图依赖）────────────────────────────


def _router_state(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "final_response": "",
        "clarification": None,
        "complexity": "deep",
    }
    base.update(overrides)
    return base


def test_route_after_router_deep_goes_escalate():
    assert route_after_router(_router_state(complexity="deep")) == "escalate"


def test_route_after_router_light_goes_skill_executor():
    assert route_after_router(_router_state(complexity="light")) == "skill_executor"


def test_route_after_router_shortcut_goes_skill_executor():
    """闸门短路（final_response 非空）即使 deep 也不升级（护栏优先）。"""
    state = _router_state(complexity="deep", final_response="合规话术")
    assert route_after_router(state) == "skill_executor"


def test_route_after_router_clarification_goes_skill_executor():
    """澄清（clarification 非空）不升级。"""
    state = _router_state(complexity="deep", clarification="请提供股票代码")
    assert route_after_router(state) == "skill_executor"


def test_route_after_escalate_fallback_goes_skill_executor():
    assert route_after_escalate({"fallback_to_skill": True}) == "skill_executor"


def test_route_after_escalate_success_goes_synth_answer():
    assert route_after_escalate({"fallback_to_skill": False}) == "synth_answer"


def test_route_after_router_non_dict_state_falls_back():
    """state 非 dict（reader 被全局 asyncio.to_thread patch 破坏）→ 保守回落 light 路径。"""
    assert route_after_router([{"ticker": "^GSPC"}]) == "skill_executor"


def test_route_after_escalate_non_dict_state_goes_synth_answer():
    """state 非 dict → escalate 分支走默认成功路径，不抛异常。"""
    assert route_after_escalate([1, 2, 3]) == "synth_answer"


# ── 图级路由语义（mock 节点函数，锁定条件边拓扑）──────────────────


def _fake_qa(complexity: str, final_response: str = "", clarification: str = ""):
    async def fake_qa(state: object) -> dict[str, object]:
        return {
            "goal": InsightGoal(
                question="分析 600519",
                intent="stock_snapshot",
                symbols=["600519"],
            ),
            "plan": "direct",
            "skill_calls": [],
            "complexity": complexity,
            "final_response": final_response,
            "clarification": clarification,
        }

    return fake_qa


def _patch_nodes(*, qa, escalate, skill, synth):
    """同时替换 chat_builder 模块命名空间的 4 个节点引用（编译时取模块全局名）。"""
    return patch.multiple(
        "aistock_agent.graph.chat_builder",
        qa_router_node=qa,
        escalate_node=escalate,
        skill_executor_node=skill,
        synth_answer_node=synth,
    )


@pytest.mark.asyncio
async def test_deep_state_routes_to_escalate():
    """deep 无短路 → escalate 执行，skill_executor 不执行；final_response/deep_source 回流。"""
    calls = {"escalate": False, "skill": False}

    async def fake_escalate(state: object) -> dict[str, object]:
        calls["escalate"] = True
        return {"final_response": "深度分析全文", "deep_source": "stock"}

    async def fake_skill(state: object) -> dict[str, object]:
        calls["skill"] = True
        return {"evidences": []}

    async def fake_synth(state: object) -> dict[str, object]:
        return {"final_response": state.get("final_response", "")}  # type: ignore[union-attr]

    with _patch_nodes(
        qa=_fake_qa(complexity="deep"),
        escalate=fake_escalate,
        skill=fake_skill,
        synth=fake_synth,
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke({"messages": [HumanMessage(content="分析 600519")]})

    assert calls["escalate"] is True
    assert calls["skill"] is False
    assert result["final_response"] == "深度分析全文"
    assert result["deep_source"] == "stock"


@pytest.mark.asyncio
async def test_light_state_routes_to_skill_executor():
    """light → skill_executor 执行，escalate 不执行。"""
    calls = {"escalate": False, "skill": False}

    async def fake_escalate(state: object) -> dict[str, object]:
        calls["escalate"] = True
        return {"final_response": "不应走到", "deep_source": "stock"}

    async def fake_skill(state: object) -> dict[str, object]:
        calls["skill"] = True
        return {"evidences": []}

    async def fake_synth(state: object) -> dict[str, object]:
        return {"final_response": state.get("final_response", "")}  # type: ignore[union-attr]

    with _patch_nodes(
        qa=_fake_qa(complexity="light"),
        escalate=fake_escalate,
        skill=fake_skill,
        synth=fake_synth,
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke({"messages": [HumanMessage(content="600519 多少钱")]})

    assert calls["escalate"] is False
    assert calls["skill"] is True
    assert result["final_response"] == ""


@pytest.mark.asyncio
async def test_escalate_fallback_routes_to_skill_executor():
    """escalate 返回 fallback_to_skill=True → 回落 skill_executor（D24），再走 synth_answer。"""
    calls = {"escalate": False, "skill": False, "synth": False}

    async def fake_escalate(state: object) -> dict[str, object]:
        calls["escalate"] = True
        return {"fallback_to_skill": True}

    async def fake_skill(state: object) -> dict[str, object]:
        calls["skill"] = True
        return {"evidences": []}

    async def fake_synth(state: object) -> dict[str, object]:
        calls["synth"] = True
        return {"final_response": state.get("final_response", "")}  # type: ignore[union-attr]

    with _patch_nodes(
        qa=_fake_qa(complexity="deep"),
        escalate=fake_escalate,
        skill=fake_skill,
        synth=fake_synth,
    ):
        graph = compile_chat_graph(checkpointer=None)
        await graph.ainvoke({"messages": [HumanMessage(content="分析 600519")]})

    assert calls["escalate"] is True
    assert calls["skill"] is True
    assert calls["synth"] is True


@pytest.mark.asyncio
async def test_shortcut_routes_to_skill_executor_not_escalate():
    """闸门短路（final_response 非空 + complexity=light）→ skill_executor，escalate 不执行。"""
    calls = {"escalate": False, "skill": False}

    async def fake_escalate(state: object) -> dict[str, object]:
        calls["escalate"] = True
        return {"final_response": "不应走到", "deep_source": "stock"}

    async def fake_skill(state: object) -> dict[str, object]:
        calls["skill"] = True
        return {"evidences": []}

    async def fake_synth(state: object) -> dict[str, object]:
        return {"final_response": state.get("final_response", "")}  # type: ignore[union-attr]

    with _patch_nodes(
        qa=_fake_qa(complexity="light", final_response="合规话术"),
        escalate=fake_escalate,
        skill=fake_skill,
        synth=fake_synth,
    ):
        graph = compile_chat_graph(checkpointer=None)
        await graph.ainvoke({"messages": [HumanMessage(content="茅台能买吗")]})

    assert calls["escalate"] is False
    assert calls["skill"] is True
