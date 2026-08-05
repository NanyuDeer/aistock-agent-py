# tests/integration/test_chat_persist_followup.py
"""P2 Task 6：升级→落库→追问 端到端集成测试（V3 全链路）。

覆盖矩阵（P2 plan §3.4 + task-6 brief Step 1，含 T6 跨任务修复验证）：
1. 登录 deep 升级 → save_analysis_report 被调（chat_analysis/today/u_42/
   update_cache=False/D18 双层）→ last_deep_report.report_id 回填 → DONE
   （on_chain_end output）携带
2. 未登录 deep 升级 → save 不调（D38）但 last_deep_report 写（report_id=None）
3. 落库失败 → 不抛异常、回答照常、report_id=None（降级不阻断）
4. 登录追问（双轮真实 checkpointer 流）→ report_lookup 读 DB → Evidence →
   回答基于 Evidence；**T6 修复验证**：追问轮按 ws.py 每轮重置 transient 信号后，
   deep_source 不再跨轮残留 → 追问不被 synth_answer deep 分支劫持，且追问轮
   不重新落库、不重写 last_deep_report
5. 未登录追问 → summary_fallback Evidence（不调 DB，D38 会话内可用）
6. 护栏/light → save 不调、last_deep_report 不写（行为与 P1 一致）
7. 负向回归守卫（复现 T5 §4.2 缺陷）：追问轮不做每轮重置 → checkpoint 残留
   deep_source="stock" → 追问被 deep 分支劫持（丢弃 Evidence、重新落库、重写
   last_deep_report）——证明 ws.py 每轮重置的必要性

约定（对齐 tests/integration/test_chat_escalate.py / test_chat_multiturn.py）：
- compile_chat_graph(checkpointer=None | MemorySaver()) 显式传 saver，不依赖
  全局 get_checkpointer()（测试不触 Redis/sqlite）
- deep 升级消息用显式 6 位代码（如 test_chat_escalate 的 _stock_qa_output）：
  中文名会命中 qa_router 闸门 2 名称解析（D36），解析失败走首轮澄清，
  不经 LLM → 无法驱动 deep 升级
- 全链路 mock：qa_router/synth_answer 的 get_quick_think/get_deep_think；
  escalate 的 ESCALATION_MAP（WorkerHandle 形状：对象带 run(state)）；
  data_client.node_api.save_analysis_report / get_analysis_report（模块级单例）
- P1 教训：mock 形状必须与真实调用路径一致——escalate 用
  `getattr(worker, "run", None) or worker` 双形态兼容，mock 必须带 .run
- ainvoke 返回的是 state 字典，其中 trace/insight/evidences 是 pydantic 对象，
  用属性访问（test_chat_escalate 同款）
"""
from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage
from langgraph.checkpoint.memory import MemorySaver

from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.graph.nodes.qa_router import QARouterOutput
from aistock_agent.graph.nodes.synth_answer import SynthInsightOutput, SynthOutput
from aistock_agent.observability.metrics import get_metrics_collector
from aistock_agent.prompts.general.system import CAPABILITY_REPLY
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.services.data_client import node_api
from aistock_agent.state.chat_schema import QuestionState
from aistock_agent.utils.date import shanghai_today

# worker 全文（deep 升级 mock 返回）；落库 D18 双层 details 即最终回答
WORKER_TEXT = (
    "深度分析全文：贵州茅台基本面扎实，估值处于历史中位数，"
    "北向资金持续净流入，行业景气度回升。"
)
DEGRADED_TEXT = "深度分析暂时不可用，请稍后重试"
DEEP_QUESTION = "深度分析一下 600519"  # 显式代码，绕过闸门 2 名称解析澄清


# ── 升级率指标隔离：模块级单例，逐用例 reset（同 test_chat_escalate） ──
@pytest.fixture(autouse=True)
def _reset_metrics() -> None:
    get_metrics_collector().reset()
    yield
    get_metrics_collector().reset()


# ── 构造 helpers ──


def _make_worker(result: dict | None = None, exc: Exception | None = None) -> MagicMock:
    """WorkerHandle 形状的 mock：`run(state) -> Awaitable[dict]`（A 契约）。"""
    worker = MagicMock()
    worker.run = AsyncMock(return_value=result, side_effect=exc)
    return worker


def _make_qa_output(
    question: str,
    intent: str,
    complexity: str,
    skill: str | None = None,
    args: dict | None = None,
    symbols: list[str] | None = None,
) -> QARouterOutput:
    """构造 QARouterOutput：plan=direct 单 Skill。"""
    return QARouterOutput(
        goal=InsightGoal(question=question, intent=intent, symbols=symbols or []),
        plan="direct",
        skill_calls=[SkillCall(skill_name=skill or intent, args=args or {})],
        complexity=complexity,  # type: ignore[arg-type]
    )


def _make_synth_output(conclusion: str, mode: str = "validate") -> SynthOutput:
    return SynthOutput(
        insight=SynthInsightOutput(
            conclusion=conclusion,
            basis_indices=[],
            confidence="medium",
            uncertainty=[],
            answer_mode=mode,  # type: ignore[arg-type]
        )
    )


def _mock_qa_llm(outputs: list[QARouterOutput]) -> MagicMock:
    """按调用顺序吐出 QARouterOutput 的 mock LLM（qa_router 用）。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[MagicMock(ainvoke=AsyncMock(return_value=out)) for out in outputs]
    )
    return mock_llm


def _mock_synth_llm(outputs: list[SynthOutput]) -> MagicMock:
    """按调用顺序吐出 SynthOutput 的 mock LLM（synth_answer light 路径用）。"""
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[MagicMock(ainvoke=AsyncMock(return_value=out)) for out in outputs]
    )
    return mock_llm


def _turn1_deep_state(message: str, user_id: str | None) -> QuestionState:
    """第 1 轮（deep 升级）初始状态：对齐 ws.py 构造（user_id 构造后追加）。"""
    return {
        "messages": [HumanMessage(content=message)],
        "goal": None,
        "plan": "direct",
        "skill_calls": [],
        "evidences": [],
        "insight": None,
        "final_response": "",
        "trace": None,
        "user_id": user_id,
    }


def _turn2_followup_state(message: str, *, reset_transients: bool = True) -> QuestionState:
    """第 2 轮（追问）初始状态。

    reset_transients=True（默认）：模拟 ws.py T6 修复的每轮入口重置
    （deep_source/final_response=None）——transient 路由信号单轮有效；
    False：复现 T5 §4.2 缺陷（transient 经 checkpointer 跨轮残留）。
    """
    state: QuestionState = {"messages": [HumanMessage(content=message)]}
    if reset_transients:
        state["deep_source"] = None
        state["final_response"] = None
    return state


def _chat_analysis_artifact(report_date: str) -> dict:
    """DB 三元组查询返回的 chat_analysis 工件（content 对齐 D18 双层）。"""
    return {
        "id": "rep_9",
        "report_type": "chat_analysis",
        "report_date": report_date,
        "content": {
            "schema_version": "2.0",
            "display_report": {
                "summary": "上次深度分析摘要：茅台基本面稳健",
                "details": WORKER_TEXT,
                "stocks": [],
                "risks": [],
            },
        },
    }


def _patch_common(worker: MagicMock, qa_llm: MagicMock, synth_llm: MagicMock | None = None):
    """qa_router/synth_answer LLM + escalate worker 公共 patch 上下文。"""
    ctxs = [
        patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=qa_llm),
        patch.dict("aistock_agent.graph.nodes.escalate.ESCALATION_MAP", {"stock": worker}),
    ]
    if synth_llm is not None:
        ctxs.append(
            patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=synth_llm)
        )
    return ctxs


# ══════════════════════════════════════════════════════════════════
# 场景 1：登录 deep 升级 → 落库 → last_deep_report 回填 → DONE 携带
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_flow_logged_in_deep_persist_and_done():
    """登录 deep 升级：save_analysis_report 被调（chat_analysis/today/u_42/
    update_cache=False/D18 双层）→ last_deep_report.report_id 回填 → DONE
    （on_chain_end output）携带 last_deep_report。"""
    saved: list[dict] = []

    async def fake_save(report_type, report_date, content, user_id=None, **kw):
        saved.append(
            {
                "report_type": report_type,
                "report_date": report_date,
                "content": content,
                "user_id": user_id,
                "update_cache": kw.get("update_cache"),
            }
        )
        return {"id": "rep_9", "report_type": report_type, "report_date": report_date}

    qa_out = _make_qa_output(
        DEEP_QUESTION, "stock_snapshot", "deep", symbols=["600519"]
    )
    qa_llm = _mock_qa_llm([qa_out])
    synth_llm = MagicMock()  # deep 分支零 LLM：若被调用即断言失败
    worker = _make_worker(result={"final_response": WORKER_TEXT})

    with ExitStack() as stack:
        for ctx in _patch_common(worker, qa_llm, synth_llm):
            stack.enter_context(ctx)
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        graph = compile_chat_graph(checkpointer=None)
        events: list[dict] = []
        async for ev in graph.astream_events(
            _turn1_deep_state(DEEP_QUESTION, user_id="u_42"),
            version="v2",
        ):
            events.append(ev)

    # escalate worker 被调；deep 分支零 LLM
    worker.run.assert_awaited_once()
    synth_llm.with_structured_output.assert_not_called()

    # 落库被调：chat_analysis / today / u_42 / update_cache=False / D18 双层
    assert len(saved) == 1
    call = saved[0]
    assert call["report_type"] == "chat_analysis"
    assert call["report_date"] == shanghai_today().isoformat()
    assert call["user_id"] == "u_42"
    assert call["update_cache"] is False
    assert call["content"]["schema_version"] == "2.0"
    display = call["content"]["display_report"]
    assert display["stocks"] == [] and display["risks"] == []

    # on_chain_end output（ws.py 取 DONE 负载的来源）携带 last_deep_report
    end_outputs = [
        ev["data"]["output"]
        for ev in events
        if ev["event"] == "on_chain_end"
        and isinstance(ev.get("data", {}).get("output"), dict)
        and ev["data"]["output"].get("final_response")
    ]
    assert end_outputs, "未捕获到 final_response 的 on_chain_end output"
    final = end_outputs[-1]
    assert final["deep_source"] == "stock"
    assert final["last_deep_report"]["report_id"] == "rep_9"
    assert final["last_deep_report"]["worker"] == "stock"
    assert final["last_deep_report"]["question"] == DEEP_QUESTION
    # D18 双层一致：summary 是最终回答前 160 字，details 是全文
    assert len(final["last_deep_report"]["summary"]) <= 160
    assert display["details"] == final["final_response"]
    assert display["summary"] == final["final_response"][:160]
    assert final["insight"].answer_mode == "deep"
    assert final["trace"].actual_mode == "deep"


# ══════════════════════════════════════════════════════════════════
# 场景 2：未登录 deep 升级 → 不落库但写 last_deep_report（report_id=None）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_flow_anonymous_no_persist_but_last_deep_report():
    """未登录（无 user_id）deep 升级：save 不调（D38）；last_deep_report 仍写（report_id=None）。"""
    called = False

    async def fake_save(*args, **kw):
        nonlocal called
        called = True
        return {"id": "x"}

    qa_out = _make_qa_output(
        DEEP_QUESTION, "stock_snapshot", "deep", symbols=["600519"]
    )
    qa_llm = _mock_qa_llm([qa_out])
    worker = _make_worker(result={"final_response": WORKER_TEXT})

    with ExitStack() as stack:
        for ctx in _patch_common(worker, qa_llm):
            stack.enter_context(ctx)
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_turn1_deep_state(DEEP_QUESTION, user_id=None))

    worker.run.assert_awaited_once()
    assert called is False  # 未登录不落库
    assert result["final_response"]
    ref = result["last_deep_report"]
    assert ref is not None
    assert ref["report_id"] is None
    assert ref["worker"] == "stock"
    assert len(ref["summary"]) <= 160


# ══════════════════════════════════════════════════════════════════
# 场景 3：落库失败 → 不抛异常、回答照常、report_id=None
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_flow_persist_failure_degrades_quietly():
    """落库抛异常 → 不抛、不阻断回答；last_deep_report 仍写（report_id=None）。"""

    async def fake_save(*args, **kw):
        raise RuntimeError("node down")

    qa_out = _make_qa_output(
        DEEP_QUESTION, "stock_snapshot", "deep", symbols=["600519"]
    )
    qa_llm = _mock_qa_llm([qa_out])
    worker = _make_worker(result={"final_response": WORKER_TEXT})

    with ExitStack() as stack:
        for ctx in _patch_common(worker, qa_llm):
            stack.enter_context(ctx)
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_turn1_deep_state(DEEP_QUESTION, user_id="u_42"))

    assert result["final_response"]  # 回答照常
    assert result["last_deep_report"] is not None
    assert result["last_deep_report"]["report_id"] is None


# ══════════════════════════════════════════════════════════════════
# 场景 4：登录追问（双轮真实 checkpointer 流）——T6 修复验证
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_flow_followup_logged_in_reads_db():
    """登录追问（双轮真实 checkpointer 流，MemorySaver）。

    T6 修复验证：追问轮按 ws.py 每轮入口重置 deep_source/final_response 后，
    checkpoint 残留的上轮 deep_source="stock" 被本轮 None 覆盖 → synth_answer
    走 light 路径消费 report_lookup 读 DB 得到的 chat_analysis Evidence，
    不被 deep 分支劫持（不丢弃 Evidence / 不重写 last_deep_report）；
    追问轮不重新落库。
    """
    saved: list[dict] = []
    get_calls: list[tuple] = []

    async def fake_save(report_type, report_date, content, user_id=None, **kw):
        saved.append((report_type, report_date, user_id))
        return {"id": "rep_9", "report_type": report_type, "report_date": report_date}

    async def fake_get(report_type, report_date, user_id=None):
        get_calls.append((report_type, report_date, user_id))
        return _chat_analysis_artifact(report_date)

    deep_out = _make_qa_output(
        DEEP_QUESTION, "stock_snapshot", "deep", symbols=["600519"]
    )
    followup_out = _make_qa_output(
        "刚才那个分析怎么样",
        "report_lookup",
        "light",
        skill="report_lookup",
        args={"report_type": "chat_analysis"},
    )
    followup_synth = _make_synth_output(
        "根据上次深度分析，贵州茅台基本面稳健、估值合理，可持续跟踪。", "validate"
    )

    qa_llm = _mock_qa_llm([deep_out, followup_out])  # 轮1 qa → 轮2 qa
    synth_llm = _mock_synth_llm([followup_synth])    # 轮1 deep 零 LLM → 轮2 light
    worker = _make_worker(result={"final_response": WORKER_TEXT})

    with ExitStack() as stack:
        for ctx in _patch_common(worker, qa_llm, synth_llm):
            stack.enter_context(ctx)
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        stack.enter_context(patch.object(node_api, "get_analysis_report", fake_get))
        # 闸门 2 名称解析打桩：避免 "刚才那个" 触发真实 Node resolve 调用
        stack.enter_context(
            patch(
                "aistock_agent.graph.nodes.qa_router.resolve_symbol",
                new=AsyncMock(return_value=None),
            )
        )
        graph = compile_chat_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "t6-followup-loggedin"}}
        # 第 1 轮：deep 升级（落库）
        result1 = await graph.ainvoke(
            _turn1_deep_state(DEEP_QUESTION, user_id="u_42"), config=config
        )
        # 第 2 轮：追问（ws.py 同款每轮重置）
        result2 = await graph.ainvoke(
            _turn2_followup_state("刚才那个分析怎么样"), config=config
        )

    # 第 1 轮：落库 + last_deep_report 回填
    assert len(saved) == 1
    assert saved[0][:2] == ("chat_analysis", shanghai_today().isoformat())
    assert result1["last_deep_report"]["report_id"] == "rep_9"

    # 第 2 轮：DB 三元组查询被调（登录态）
    assert len(get_calls) == 1
    assert get_calls[0][0] == "chat_analysis"
    assert get_calls[0][2] == "u_42"

    # 第 2 轮：回答基于 chat_analysis Evidence，未被 deep 分支劫持
    assert result2["final_response"]
    assert DEGRADED_TEXT not in result2["final_response"]
    assert "根据上次深度分析" in result2["final_response"]
    assert result2["trace"].actual_mode != "deep"
    assert result2["insight"].answer_mode != "deep"
    assert result2["deep_source"] is None  # transient 已按轮清空
    ev = result2["trace"].evidences[0]
    assert ev.skill_name == "report_lookup"
    assert ev.degraded is False
    assert any("上次深度分析" in f for f in ev.facts)

    # 追问轮不重新落库；last_deep_report 不被重写（保持第 1 轮 rep_9 引用）
    assert len(saved) == 1
    assert result2["last_deep_report"]["report_id"] == "rep_9"
    assert result2["last_deep_report"]["question"] == DEEP_QUESTION


# ══════════════════════════════════════════════════════════════════
# 场景 5：未登录追问 → summary_fallback Evidence（不调 DB）
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_flow_followup_anonymous_session_fallback():
    """未登录追问：get_analysis_report 不调（D38），summary_fallback 构造
    Evidence（会话内摘要），回答不中断。"""
    save_called = False
    get_calls: list[tuple] = []

    async def fake_save(*args, **kw):
        nonlocal save_called
        save_called = True
        return {"id": "x"}

    async def fake_get(report_type, report_date, user_id=None):
        get_calls.append((report_type, report_date, user_id))
        return _chat_analysis_artifact(report_date)

    deep_out = _make_qa_output(
        DEEP_QUESTION, "stock_snapshot", "deep", symbols=["600519"]
    )
    followup_out = _make_qa_output(
        "刚才那个分析怎么样",
        "report_lookup",
        "light",
        skill="report_lookup",
        args={"report_type": "chat_analysis"},
    )
    followup_synth = _make_synth_output(
        "根据会话内摘要，上次对贵州茅台的深度分析结论是基本面稳健。", "validate"
    )

    qa_llm = _mock_qa_llm([deep_out, followup_out])
    synth_llm = _mock_synth_llm([followup_synth])
    worker = _make_worker(result={"final_response": WORKER_TEXT})

    with ExitStack() as stack:
        for ctx in _patch_common(worker, qa_llm, synth_llm):
            stack.enter_context(ctx)
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        stack.enter_context(patch.object(node_api, "get_analysis_report", fake_get))
        stack.enter_context(
            patch(
                "aistock_agent.graph.nodes.qa_router.resolve_symbol",
                new=AsyncMock(return_value=None),
            )
        )
        graph = compile_chat_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "t6-followup-anon"}}
        # 第 1 轮：匿名 deep 升级（不落库，但写 last_deep_report）
        result1 = await graph.ainvoke(
            _turn1_deep_state(DEEP_QUESTION, user_id=None), config=config
        )
        # 第 2 轮：未登录追问
        result2 = await graph.ainvoke(
            _turn2_followup_state("刚才那个分析怎么样"), config=config
        )

    # 第 1 轮：不落库；last_deep_report 写（report_id=None）
    assert save_called is False
    assert result1["last_deep_report"]["report_id"] is None

    # 第 2 轮：不调 DB；Evidence 来自会话内摘要
    assert get_calls == []
    assert result2["final_response"]
    assert DEGRADED_TEXT not in result2["final_response"]
    assert "根据会话内摘要" in result2["final_response"]
    assert result2["trace"].actual_mode != "deep"
    ev = result2["trace"].evidences[0]
    assert ev.skill_name == "report_lookup"
    assert ev.degraded is False
    # facts = summary_fallback（last_deep_report.summary = 上轮回答前 160 字）
    assert any("深度分析全文" in f for f in ev.facts)
    # 追问轮不重新落库
    assert save_called is False


# ══════════════════════════════════════════════════════════════════
# 场景 6：护栏/light → 不落库、不写 last_deep_report
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_guardrail_and_light_do_not_persist():
    """护栏（寒暄闸门短路）与 light 快答：save 不调、last_deep_report 不写。"""
    save_called = False

    async def fake_save(*args, **kw):
        nonlocal save_called
        save_called = True
        return {"id": "x"}

    # ── 闸门 0.5：寒暄短路（零 LLM，直接透出话术）──
    gate_llm = MagicMock()
    with ExitStack() as stack:
        stack.enter_context(
            patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=gate_llm)
        )
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        graph = compile_chat_graph(checkpointer=None)
        result = await graph.ainvoke(_turn1_deep_state("你好", user_id="u_42"))

    gate_llm.with_structured_output.assert_not_called()  # 闸门短路不进 LLM
    assert result["final_response"] == CAPABILITY_REPLY
    assert save_called is False
    assert result.get("last_deep_report") is None

    # ── light 快答（LLM 判定 light → skill_executor → synth LLM）──
    light_out = _make_qa_output(
        "600519 现在多少钱",
        "stock_snapshot",
        "light",
        skill="stock_snapshot",
        args={"symbol": "600519"},
        symbols=["600519"],
    )
    light_synth = _make_synth_output("茅台当前 1800 元，股价平稳。", "validate")
    qa_llm = _mock_qa_llm([light_out])
    synth_llm = _mock_synth_llm([light_synth])
    fake_get_quote = MagicMock()
    fake_get_quote.ainvoke = AsyncMock(return_value="600519 当前价 1800")

    with ExitStack() as stack:
        for ctx in _patch_common(MagicMock(), qa_llm, synth_llm):
            stack.enter_context(ctx)
        stack.enter_context(
            patch("aistock_agent.skills.stock_snapshot.get_quote", new=fake_get_quote)
        )
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        graph = compile_chat_graph(checkpointer=None)
        result2 = await graph.ainvoke(_turn1_deep_state("600519 现在多少钱", user_id="u_42"))

    assert result2["final_response"].startswith("茅台当前 1800 元")
    assert save_called is False
    assert result2.get("last_deep_report") is None


# ══════════════════════════════════════════════════════════════════
# 场景 7：负向回归守卫——不做每轮重置 → deep_source 跨轮残留 → 追问被劫持
# ══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_followup_without_per_turn_reset_reproduces_hijack():
    """负向回归守卫（复现 T5 §4.2 缺陷）：追问轮不重置 transient 信号 →
    checkpoint 残留 deep_source="stock" + final_response=上轮全文 →
    synth_answer deep 分支劫持追问：丢弃 report_lookup Evidence、重新落库、
    重写 last_deep_report。

    该用例固化缺陷行为，证明 ws.py 每轮重置（T6 修复）的必要性：
    一旦移除重置，本用例保持通过而正向往复测（场景 4）转红。
    """
    saved: list[dict] = []

    async def fake_save(report_type, report_date, content, user_id=None, **kw):
        saved.append((report_type, report_date, user_id))
        return {"id": "rep_9", "report_type": report_type, "report_date": report_date}

    deep_out = _make_qa_output(
        DEEP_QUESTION, "stock_snapshot", "deep", symbols=["600519"]
    )
    followup_out = _make_qa_output(
        "刚才那个分析怎么样",
        "report_lookup",
        "light",
        skill="report_lookup",
        args={"report_type": "chat_analysis"},
    )
    followup_synth = _make_synth_output("不应被输出：Evidence 被 deep 分支丢弃", "validate")

    qa_llm = _mock_qa_llm([deep_out, followup_out])
    synth_llm = _mock_synth_llm([followup_synth])
    worker = _make_worker(result={"final_response": WORKER_TEXT})

    with ExitStack() as stack:
        for ctx in _patch_common(worker, qa_llm, synth_llm):
            stack.enter_context(ctx)
        stack.enter_context(patch.object(node_api, "save_analysis_report", fake_save))
        stack.enter_context(patch.object(node_api, "get_analysis_report", _chat_analysis_artifact))
        stack.enter_context(
            patch(
                "aistock_agent.graph.nodes.qa_router.resolve_symbol",
                new=AsyncMock(return_value=None),
            )
        )
        graph = compile_chat_graph(checkpointer=MemorySaver())
        config = {"configurable": {"thread_id": "t6-followup-noreset"}}
        # 第 1 轮 deep 升级（其结果/checkpoint 是缺陷的前提）
        await graph.ainvoke(
            _turn1_deep_state(DEEP_QUESTION, user_id="u_42"), config=config
        )
        # 追问轮不做每轮重置（transient 经 checkpointer 跨轮残留）
        result2 = await graph.ainvoke(
            _turn2_followup_state("刚才那个分析怎么样", reset_transients=False),
            config=config,
        )

    # 缺陷行为：追问被 deep 分支劫持
    assert result2["trace"].actual_mode == "deep"
    assert result2["insight"].answer_mode == "deep"
    # Evidence 被丢弃：synth LLM 结论未出现在回答中
    assert "不应被输出" not in result2["final_response"]
    # 追问轮重新落库（缺陷）
    assert len(saved) == 2
    # last_deep_report 被重写为追问轮引用（question 换成追问问题）
    assert result2["last_deep_report"]["question"] == "刚才那个分析怎么样"
