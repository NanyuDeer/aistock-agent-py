"""CHAT QA P1 升级路径端到端集成测试（Task 6）。

覆盖：
1. light 路径不变（skill_executor → synth LLM，escalate worker 不调用）
2. deep 路径 escalate → synth deep 分支（D28 风险段 + actual_mode=deep + 零 LLM + 升级率指标）
3. sector tag_code 未命中 → escalate fallback_to_skill → skill_executor（skill 快答）
4. force_deep=True + LLM 判定 light → qa_router 强制 deep → 升级到 worker
5. worker 抛异常 → escalate 降级文本经 synth deep 分支统一出口（不中断）
6. D3 SSE 透传：嵌套 worker（真实 create_react_agent + fake 流式 LLM）的
   on_chat_model_stream / on_tool_start / on_tool_end 冒泡到顶层 graph.astream_events

约定（对齐既有 tests/integration/test_chat_e2e_*.py）：
- compile_chat_graph(checkpointer=None)，ainvoke / astream_events 驱动
- 全链路 mock：qa_router/synth_answer 的 get_quick_think / get_deep_think，
  escalate 的 ESCALATION_MAP（WorkerHandle 形状：对象带 run(state) 方法）
"""
from __future__ import annotations

import json
import re
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, AIMessageChunk, HumanMessage
from langchain_core.messages.tool import ToolCallChunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.graph.nodes.qa_router import QARouterOutput
from aistock_agent.graph.nodes.synth_answer import SynthInsightOutput, SynthOutput
from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.prompts.general.system import RISK_DISCLAIMER
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState


# ── 升级率指标隔离：模块级单例，逐用例 reset ──
@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    get_metrics_collector().reset()
    yield
    get_metrics_collector().reset()


def _build_state(message: str) -> QuestionState:
    return {
        "messages": [HumanMessage(content=message)],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
    }


def _make_worker(result: dict | None = None, exc: Exception | None = None) -> MagicMock:
    """构造 WorkerHandle 形状的 mock：`run(state) -> Awaitable[dict]`（A 契约）。"""
    worker = MagicMock()
    worker.run = AsyncMock(return_value=result, side_effect=exc)
    return worker


def _stock_qa_output(question: str, complexity: str) -> QARouterOutput:
    return QARouterOutput(
        goal=InsightGoal(
            question=question,
            intent="stock_snapshot",
            symbols=["600519"],
        ),
        plan="direct",
        skill_calls=[SkillCall(skill_name="stock_snapshot", args={"symbol": "600519"})],
        complexity=complexity,
    )


def _mock_llm_with_outputs(qa_output: QARouterOutput, synth_output: SynthOutput) -> MagicMock:
    """构造同时服务 qa_router 与 synth_answer 的 mock LLM（调用顺序：qa → synth）。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[
            MagicMock(ainvoke=AsyncMock(return_value=qa_output)),
            MagicMock(ainvoke=AsyncMock(return_value=synth_output)),
        ]
    )
    return mock_llm


def _mock_quick_only(qa_output: QARouterOutput) -> MagicMock:
    """仅服务 qa_router 的 mock LLM（deep 分支 synth 零 LLM，不消费第二个输出）。"""
    mock_quick = MagicMock()
    mock_quick.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=AsyncMock(return_value=qa_output))
    )
    return mock_quick


# ══════════════════════════════════════════════════════════════════
# Step 1：升级路径端到端
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_light_path_unchanged():
    """complexity=light → skill_executor → synth LLM 路径（既有行为，升级不侵入）。"""
    qa_output = _stock_qa_output("600519 现在多少钱", "light")
    synth_output = SynthOutput(
        insight=SynthInsightOutput(
            conclusion="茅台当前 1800 元",
            basis_indices=[1],
            confidence="medium",
            uncertainty=[],
            answer_mode="validate",
        )
    )
    mock_llm = _mock_llm_with_outputs(qa_output, synth_output)
    escalate_worker = _make_worker(result={"final_response": "不应被调用"})
    # skill 以 Runnable 风格调用 get_quote.ainvoke({"symbol": ...})（见 stock_snapshot.py）
    fake_get_quote = MagicMock()
    fake_get_quote.ainvoke = AsyncMock(return_value="600519 当前价 1800")

    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
        ),
        patch(
            "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
        ),
        patch.dict(
            "aistock_agent.graph.nodes.escalate.ESCALATION_MAP",
            {"stock": escalate_worker},
        ),
        patch(
            "aistock_agent.skills.stock_snapshot.get_quote",
            new=fake_get_quote,
        ),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_build_state("600519 现在多少钱"))

    # skill_executor 执行 + synth LLM 产出（LLM 路径）
    assert result["evidences"][0].skill_name == "stock_snapshot"
    assert result["final_response"].startswith("茅台当前 1800 元")
    assert result["trace"].actual_mode == "validate"
    # escalate worker 未被调用；升级率指标不计数
    escalate_worker.run.assert_not_awaited()
    assert get_metrics_collector().get_metrics()["chat_qa"]["escalation_total"] == {}


@pytest.mark.asyncio
async def test_deep_path_escalates_to_synth():
    """complexity=deep（stock 意图）→ escalate（mock worker 返回全文）→ synth deep 分支。

    断言：final_response = worker 全文 + D28 风险段；deep 分支零 LLM；
    trace.actual_mode == "deep"；升级率指标 stock +1。
    """
    qa_output = _stock_qa_output("深度分析一下 600519", "deep")
    mock_quick = _mock_quick_only(qa_output)
    mock_deep = MagicMock()
    worker = _make_worker(result={"final_response": "深度分析全文：贵州茅台基本面扎实。"})

    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_quick
        ),
        patch(
            "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_deep
        ),
        patch.dict(
            "aistock_agent.graph.nodes.escalate.ESCALATION_MAP",
            {"stock": worker},
        ),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_build_state("深度分析一下 600519"))

    worker.run.assert_awaited_once()
    # 升级路径：escalate 直调 worker，skill_executor 不执行
    assert result["skill_calls"][0].skill_name == "stock_snapshot"
    assert result["evidences"] == []
    # worker 全文经 synth deep 分支加工：D28 风险段拼接
    final = result["final_response"]
    assert "深度分析全文" in final
    assert RISK_DISCLAIMER in final
    assert result["deep_source"] == "stock"
    assert result["trace"].actual_mode == "deep"
    assert result["insight"].answer_mode == "deep"
    # deep 分支零 LLM（synth_answer 未调 get_deep_think）
    mock_deep.assert_not_called()
    # 升级率指标：stock +1（T6 基础计数，escalate 节点记录）
    metrics = get_metrics_collector().get_metrics()
    assert metrics["chat_qa"]["escalation_total"] == {"stock": 1}


@pytest.mark.asyncio
async def test_sector_fallback_routes_to_skill_executor():
    """sector tag_code 未命中 → escalate fallback_to_skill → skill_executor（skill 快答）。"""
    qa_output = QARouterOutput(
        goal=InsightGoal(question="分析一下未知板块", intent="sector_snapshot"),
        plan="direct",
        skill_calls=[SkillCall(skill_name="sector_snapshot", args={})],
        complexity="deep",
    )
    synth_output = SynthOutput(
        insight=SynthInsightOutput(
            conclusion="该板块暂未收录，可参考市场整体表现",
            basis_indices=[1],
            confidence="medium",
            uncertainty=[],
            answer_mode="validate",
        )
    )
    mock_llm = _mock_llm_with_outputs(qa_output, synth_output)
    worker = _make_worker(result={"final_response": "不应被调用"})
    mock_api = MagicMock()
    mock_api.get = AsyncMock(
        return_value={
            "update_time": "2026-07-30 10:30",
            "hot_sectors": [
                {
                    "name": "半导体",
                    "today_change": 3.2,
                    "leading_stock": "中芯国际",
                    "main_stocks": [
                        {"code": "688981", "name": "中芯国际", "change_pct": 8.5}
                    ],
                }
            ],
        }
    )

    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm
        ),
        patch(
            "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm
        ),
        patch(
            "aistock_agent.graph.nodes.qa_router.resolve_symbol",
            new=AsyncMock(return_value=None),
        ),
        patch.dict(
            "aistock_agent.graph.nodes.escalate.ESCALATION_MAP",
            {"sector": worker},
        ),
        # 板块名未命中 tag_code（D24）→ escalate 回落 skill_executor
        patch("aistock_agent.graph.nodes.escalate.resolve_tag_code", return_value=None),
        patch("aistock_agent.skills.sector_snapshot.node_api", new=mock_api),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_build_state("分析一下未知板块"))

    # worker 未调用（回落）；skill_executor 执行 sector_snapshot 快答
    worker.run.assert_not_awaited()
    assert result["evidences"][0].skill_name == "sector_snapshot"
    assert result["evidences"][0].degraded is False
    # synth LLM 路径产出（回落走后端 skill 快答）
    assert result["final_response"].startswith("该板块暂未收录")
    assert result["trace"].actual_mode == "validate"
    # 回落未升级 → 升级率指标不计数
    assert get_metrics_collector().get_metrics()["chat_qa"]["escalation_total"] == {}


@pytest.mark.asyncio
async def test_force_deep_forces_upgrade():
    """force_deep=True + LLM 判定 light → qa_router 强制 deep → escalate 升级到 worker。"""
    qa_output = _stock_qa_output("600519 今天怎么样", "light")
    mock_quick = _mock_quick_only(qa_output)
    mock_deep = MagicMock()
    worker = _make_worker(result={"final_response": "深度分析全文（force_deep）"})

    state = _build_state("600519 今天怎么样")
    state["force_deep"] = True  # ws.py 在构造 state 后追加（§3.1：签名不变）

    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_quick
        ),
        patch(
            "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_deep
        ),
        patch.dict(
            "aistock_agent.graph.nodes.escalate.ESCALATION_MAP",
            {"stock": worker},
        ),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(state)

    # LLM 判定 light 但 force_deep=True → 强制升级
    worker.run.assert_awaited_once()
    assert result["deep_source"] == "stock"
    assert result["trace"].actual_mode == "deep"
    assert "force_deep" in result["final_response"]
    mock_deep.assert_not_called()
    assert get_metrics_collector().get_metrics()["chat_qa"]["escalation_total"] == {
        "stock": 1
    }


@pytest.mark.asyncio
async def test_deep_worker_exception_degraded():
    """worker 抛异常 → escalate 降级文本 → synth deep 分支统一出口（不中断、零 LLM）。"""
    qa_output = _stock_qa_output("深度分析一下 600519", "deep")
    mock_quick = _mock_quick_only(qa_output)
    mock_deep = MagicMock()
    worker = _make_worker(exc=RuntimeError("worker boom"))

    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_quick
        ),
        patch(
            "aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_deep
        ),
        patch.dict(
            "aistock_agent.graph.nodes.escalate.ESCALATION_MAP",
            {"stock": worker},
        ),
    ):
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_build_state("深度分析一下 600519"))

    # 不中断，降级文本经 deep 分支输出
    assert result["final_response"]
    assert "深度分析暂时不可用" in result["final_response"]
    assert result["deep_source"] == "stock"
    assert result["trace"].actual_mode == "deep"
    assert result["insight"].answer_mode == "deep"
    mock_deep.assert_not_called()


# ══════════════════════════════════════════════════════════════════
# Step 2：D3 SSE 透传验证（真实 create_react_agent 嵌套 worker）
# ══════════════════════════════════════════════════════════════════


@tool
def _fake_quote(symbol: str) -> str:
    """Fake quote lookup（SSE 透传测试用，不触网）。"""
    return f"{symbol} 当前价 100"


class _FakeReactLLM(GenericFakeChatModel):
    """GenericFakeChatModel 在 langchain-core 0.3.58 的两个缺口（真实 create_react_agent 需要）：

    1. bind_tools raise NotImplementedError → 返回 self（fake 模型工具调用由预设消息驱动）
    2. _stream 对空 content 的 tool_call 消息零产出 → "No generations found in stream"；
       补上 tool_call_chunks 的 chunk 产出，保证流式路径有生成。
    """

    def bind_tools(self, tools, **kwargs):  # noqa: ARG002
        return self

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        chat_result = self._generate(
            messages, stop=stop, run_manager=run_manager, **kwargs
        )
        message = chat_result.generations[0].message
        if message.tool_calls:
            chunks = [
                ToolCallChunk(
                    name=tc["name"],
                    args=json.dumps(tc["args"], ensure_ascii=False),
                    id=tc.get("id"),
                    index=i,
                )
                for i, tc in enumerate(message.tool_calls)
            ]
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(
                    content="", tool_call_chunks=chunks, id=message.id
                )
            )
            if run_manager:
                run_manager.on_llm_new_token("", chunk=chunk)
            yield chunk
            return
        content = message.content
        if content:
            for token in re.split(r"(\s)", content):
                chunk = ChatGenerationChunk(
                    message=AIMessageChunk(content=token, id=message.id)
                )
                if run_manager:
                    run_manager.on_llm_new_token(token, chunk=chunk)
                yield chunk
        else:
            chunk = ChatGenerationChunk(
                message=AIMessageChunk(content="", id=message.id)
            )
            if run_manager:
                run_manager.on_llm_new_token("", chunk=chunk)
            yield chunk


def _make_fake_react_llm() -> _FakeReactLLM:
    """预设两轮输出：第一轮 tool_call（_fake_quote），第二轮最终全文。"""
    return _FakeReactLLM(
        messages=iter(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "_fake_quote",
                            "args": {"symbol": "600519"},
                            "id": "call_1",
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="深度分析全文：贵州茅台基本面扎实。"),
            ]
        )
    )


@pytest.mark.asyncio
async def test_sse_bubbling_nested_worker_events():
    """D3：worker 内部嵌套 run 的事件冒泡到顶层 graph.astream_events(version="v2")。

    以真实 create_react_agent（fake 流式 LLM + 真实 tool）作为 stock worker，
    断言顶层事件流包含嵌套执行的 on_chat_model_stream（text）与
    on_tool_start / on_tool_end —— SSE 透传成立，ws.py 无需额外事件转发。
    """
    fake_llm = _make_fake_react_llm()
    nested_agent = create_react_agent(fake_llm, [_fake_quote])

    async def fake_stock_worker(state):
        result = await nested_agent.ainvoke(
            {"messages": [HumanMessage(content=f"分析 {state.get('symbol')}")]}
        )
        return {"final_response": result["messages"][-1].content}

    worker_mock = MagicMock()
    worker_mock.run = fake_stock_worker

    qa_output = _stock_qa_output("深度分析一下 600519", "deep")
    mock_quick = _mock_quick_only(qa_output)

    with (
        patch(
            "aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_quick
        ),
        patch.dict(
            "aistock_agent.graph.nodes.escalate.ESCALATION_MAP",
            {"stock": worker_mock},
        ),
    ):
        graph = compile_chat_graph(checkpointer=None)
        events: list[dict] = []
        async for ev in graph.astream_events(_build_state("深度分析一下 600519"), version="v2"):
            events.append(ev)

    names = [ev["event"] for ev in events]
    stream_texts = [
        ev.get("data", {}).get("chunk", "").content
        for ev in events
        if ev["event"] == "on_chat_model_stream"
    ]

    # 顶层事件流包含嵌套 worker 的模型流式事件（text 非空）
    assert "on_chat_model_stream" in names
    assert any(isinstance(t, str) and t.strip() for t in stream_texts)
    # 嵌套 worker 的工具执行事件冒泡（fake_quote 为 worker 内部 tool）
    tool_events = [
        ev for ev in events if ev["event"] in ("on_tool_start", "on_tool_end")
    ]
    assert len(tool_events) == 2
    assert {ev.get("name") for ev in tool_events} == {"_fake_quote"}
    # 事件来源是嵌套 run（run_id 与顶层不同），证明来自 worker 内部
    assert any(ev.get("run_id") for ev in tool_events)
