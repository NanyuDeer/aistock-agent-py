"""G1 集成验证（层 3，必选）：MemorySaver 真实图同 thread 两轮（deep→light）。

落地方式（完整实现）：真实 ``compile_chat_graph(checkpointer=MemorySaver())`` +
``graph.astream`` 两轮驱动（同 thread_id），MemorySaver 驱动真实 state 合并语义。
- qa_router / escalate / skill_executor 三个前置节点 mock（避免真实意图路由 LLM
  与外部 worker 服务，测试环境不可达）；
- synth_answer 用**真实节点**：deep 分支为纯代码路径（deep_source 非 None 时不触发
  LLM，真实写 last_deep_report），light 分支仅 mock 模块内
  ``get_deep_think`` / ``with_chat_structured_output``（避免外部 API），保留真实
  返回 dict 键行为（light 分支不写 last_deep_report 键）。
- user_id 置 None → _persist_chat_analysis 直接短路，不触外部 node_api。

断言：
1. deep 轮输出 last_deep_report 非 None、cards 非 None（D12/D13 引用 + P11 卡片）；
2. light 轮输出不含 last_deep_report 键、cards None（非 deep 不写键）；
3. light 轮后 graph.aget_state 的 last_deep_report 仍非 None（LastValue 通道不被
   非 deep 轮覆盖 → T5 追问保留）；cards 已归零（transient 语义）。
"""
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.schemas.chat_contract import InsightGoal

_THREAD_ID = "g1-checkpoint-tt1"


class _FakeInsightOutput:
    """synth_answer light 分支结构化输出（确定性，覆盖 SynthOutput.insight 读取字段）。"""

    conclusion = "普通回答"
    basis_indices = []
    confidence = "low"
    uncertainty = []
    answer_mode = "validate"


class _FakeSynthOutput:
    """覆盖 ``output.insight`` 的假 SynthOutput（with_chat_structured_output 返回）。"""

    insight = _FakeInsightOutput()


async def _fake_structured_ainvoke(*args, **kwargs) -> _FakeSynthOutput:
    return _FakeSynthOutput()


def _fake_structured_llm(llm, schema, **kwargs) -> MagicMock:
    """mock with_chat_structured_output：返回 ainvoke 产出确定性 SynthOutput 的 Runnable。"""
    runnable = MagicMock()
    runnable.ainvoke = _fake_structured_ainvoke
    return runnable


# 可变路由配置：deep 轮走 escalate，light 轮走 skill_executor（单次编译复用）
_route_cfg: dict[str, object] = {"complexity": "deep"}


async def _fake_qa(state: object) -> dict[str, object]:
    """mock qa_router：写 goal + complexity（读取 _route_cfg 控制 deep/light 路由）。"""
    return {
        "goal": InsightGoal(
            question="深度分析贵州茅台", intent="stock_snapshot", symbols=["600519"]
        ),
        "plan": "direct",
        "skill_calls": [],
        "complexity": _route_cfg["complexity"],
        "final_response": "",
    }


async def _fake_escalate(state: object) -> dict[str, object]:
    """mock escalate（仅 deep 轮执行）：回流 worker 全文 + deep_source。"""
    return {"deep_source": "stock", "final_response": "深度分析全文（worker 产出）"}


async def _fake_skill(state: object) -> dict[str, object]:
    """mock skill_executor（仅 light 轮执行）：空证据，走 synth_answer 轻量路径。"""
    return {"evidences": []}


def _initial_state(message: str) -> dict[str, object]:
    """构造每轮初始状态（含 transient 归零，对齐 routes.py reset_transient_state）。

    deep_source/goals/general_source 等 LastValue 通道在真实场景由 API 入口
    reset_transient_state 归零；此处显式重置，防止 checkpoint 跨轮残留导致
    light 轮被误判为 deep。
    """
    return {
        "messages": [HumanMessage(content=message)],
        "deep_source": None,
        "final_response": None,
        "goals": None,
        "general_source": None,
        "confirm": None,
        "confirm_choice": None,
        "confirm_timeout": None,
    }


async def _run_round(graph, message: str) -> dict[str, object]:
    """跑一轮 astream，返回 synth_answer 末节点输出（终态）。"""
    updates: dict[str, object] = {}
    async for step in graph.astream(
        _initial_state(message),
        config={"configurable": {"thread_id": _THREAD_ID}},
        stream_mode="updates",
    ):
        if isinstance(step, dict) and "synth_answer" in step:
            updates = step["synth_answer"]
    return updates


@pytest.mark.asyncio
async def test_deep_then_light_rounds_keep_checkpoint_ref():
    """G1 三层核心：deep 轮写引用 → light 轮不覆盖 → checkpoint 保留非 None（T5 不退化）。"""
    saver = MemorySaver()

    with patch.multiple(
        "aistock_agent.graph.chat_builder",
        qa_router_node=_fake_qa,
        escalate_node=_fake_escalate,
        skill_executor_node=_fake_skill,
    ), patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think"
    ) as _mock_get_deep_think, patch(
        "aistock_agent.graph.nodes.synth_answer.with_chat_structured_output",
        side_effect=_fake_structured_llm,
    ):
        graph = compile_chat_graph(checkpointer=saver)

        # ── 第 1 轮：deep ──
        _route_cfg["complexity"] = "deep"
        deep_output = await _run_round(graph, "深度分析贵州茅台")

        # 1. deep 轮：last_deep_report 非 None + cards 非 None
        assert deep_output.get("last_deep_report") is not None
        assert deep_output["last_deep_report"]["worker"] == "stock"
        assert deep_output.get("cards") is not None

        # ── 第 2 轮：light（同 thread，T5 追问）──
        _route_cfg["complexity"] = "light"
        light_output = await _run_round(graph, "市盈率是什么")

        # 2. light 轮：不写 last_deep_report 键 + cards None
        assert "last_deep_report" not in light_output
        assert light_output.get("cards") is None

        # ── checkpoint 断言（核心验证点）──
        final_state = await graph.aget_state(
            config={"configurable": {"thread_id": _THREAD_ID}}
        )
        # 3a. LastValue 通道：非 deep 轮不覆盖 → 引用保留
        assert final_state.values["last_deep_report"] is not None
        assert final_state.values["last_deep_report"]["worker"] == "stock"
        # 3b. cards 为 transient（非 deep 轮写 None 清除通道，不残留 deep 轮卡片）——
        #     与 last_deep_report 的持久语义区分（.get 兼容"键缺失"与"值 None"两种形态）
        assert final_state.values.get("cards") is None
