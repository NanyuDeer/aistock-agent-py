# tests/integration/test_phase5_long_session_smoke.py
"""Phase 5 长会话集成冒烟（Task 3）— 真实临时 sqlite checkpointer + mock LLM。

覆盖 plan Task 3 Step 3 三项（全图连跑多轮，mock LLM 防真实调用）：
1. **>12 条消息**：连跑 7 轮（13 条 messages）走 chat 图 →
   - 回答正常（final_response 非空）
   - 第 7 轮 qa_router LLM 收到 12 条窗口（llm_messages = [prompt] + 最近 12 条）
   - prompt 含"此前对话摘要"（超窗零 LLM 确定性摘要注入，被挤出历史收敛其中）
   - checkpointer 持久化 messages_summary
2. **删会话**：delete_thread(thread_id) → temp sqlite 无该 thread（aget_tuple None）
3. **短会话字节不变**：1 轮（≤12 条）→ LLM prompt 不含"此前对话摘要"，
   messages_summary 不持久化

注意（既有项目教训）：checkpointer 是模块级单例，teardown 必须重置
_checkpointer/_checkpointer_cm/_sqlite_conn_atexit_registered 三项；每个线程用
独立 thread_id + 独立临时 sqlite 文件，不依赖 .langgraph.db 跨运行累积。
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import HumanMessage

from aistock_agent.config import settings
from aistock_agent.graph.chat_builder import compile_chat_graph
from aistock_agent.graph.nodes.qa_router import QARouterOutput
from aistock_agent.graph.nodes.synth_answer import SynthInsightOutput, SynthOutput
from aistock_agent.memory import checkpointer as cp_module
from aistock_agent.memory.checkpointer import delete_thread, get_checkpointer
from aistock_agent.schemas.chat_contract import InsightGoal, SkillCall
from aistock_agent.state.chat_schema import QuestionState

TOTAL_TURNS = 7  # 13 条 messages（7H + 6A），超窗 12 条


def _qa_output(intent: str, skill: str, args: dict | None = None, **goal_kwargs) -> QARouterOutput:
    return QARouterOutput(
        goal=InsightGoal(question="600519 现在多少钱", intent=intent, **goal_kwargs),
        plan="direct",
        skill_calls=[SkillCall(skill_name=skill, args=args or {})],
        complexity="light",
    )


def _synth_output(conclusion: str, mode: str = "validate") -> SynthOutput:
    return SynthOutput(
        insight=SynthInsightOutput(
            conclusion=conclusion,
            basis_indices=[1],
            confidence="medium",
            uncertainty=[],
            answer_mode=mode,
        )
    )


def _thread_cfg(thread_id: str) -> dict[str, dict[str, str]]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}


def _base_state(message: str) -> QuestionState:
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


@pytest.fixture()
def sqlite_checkpointer(tmp_path):
    """临时 sqlite checkpointer 单例（指向 tmp 文件），teardown 重置单例并关连接。"""
    original_backend = settings.checkpointer_backend
    original_path = settings.sqlite_path
    cp_module._checkpointer = None
    cp_module._checkpointer_cm = None
    cp_module._sqlite_conn_atexit_registered = False
    settings.checkpointer_backend = "sqlite"
    settings.sqlite_path = str(tmp_path / "smoke.langgraph.db")
    saver = None
    try:
        saver = get_checkpointer()
        yield saver
    finally:
        if saver is not None:
            conn = getattr(saver, "conn", None)
            if conn is not None:
                try:
                    cp_module._run_coro_sync(conn.close())
                except Exception:
                    pass
        cp_module._checkpointer = None
        cp_module._checkpointer_cm = None
        cp_module._sqlite_conn_atexit_registered = False
        settings.checkpointer_backend = original_backend
        settings.sqlite_path = original_path


@pytest.fixture()
def mock_chat_llm():
    """构造每轮 qa_router/synth_answer 交替返回固定输出的 mock LLM。

    with_structured_output 按调用顺序返回 qa/synth 结构化 mock；
    qa 的 ainvoke 记录收到的 llm_messages（供窗口/摘要断言），synth 固定返回结论。
    """
    qa_out = _qa_output(
        "stock_snapshot", "stock_snapshot", {"symbol": "600519"}, symbols=["600519"]
    )
    synth_out = _synth_output("茅台当前 1800 元")

    captured: list = []

    async def qa_ainvoke(messages):
        captured.append(list(messages))
        return qa_out

    qa_structured = MagicMock(ainvoke=qa_ainvoke)
    synth_structured = MagicMock(ainvoke=AsyncMock(return_value=synth_out))
    mock_llm = MagicMock()
    mock_llm.with_structured_output = MagicMock(
        side_effect=[qa_structured, synth_structured] * TOTAL_TURNS
    )
    return mock_llm, captured


@pytest.fixture()
def chat_graph_patches(mock_chat_llm):
    """与 test_chat_multiturn 一致的 patch 上下文（LLM + 交易日 + skill 取数）。"""
    mock_llm, captured = mock_chat_llm
    patchers = [
        patch("aistock_agent.graph.nodes.qa_router.get_quick_think", return_value=mock_llm),
        patch("aistock_agent.graph.nodes.synth_answer.get_deep_think", return_value=mock_llm),
        patch(
            "aistock_agent.graph.nodes.synth_answer.trading_session_status",
            return_value=("trading", ""),
        ),
        # get_quote 是 @tool 结构化工具，真实调用形状为 await get_quote.ainvoke({...})
        patch(
            "aistock_agent.skills.stock_snapshot.get_quote",
            new=MagicMock(ainvoke=AsyncMock(return_value="600519 当前价 1800")),
        ),
        # stock_snapshot.py 模块级 import 的 node_api 单例（真实 I/O 路径）：
        # patch 其模块引用消除 /internal/quote 网络请求（失败仅被吞异常侥幸通过）
        patch(
            "aistock_agent.skills.stock_snapshot.node_api",
            new=MagicMock(
                get=AsyncMock(
                    return_value={
                        "股票简称": "贵州茅台",
                        "股票代码": "600519",
                        "最新价": 1800.0,
                        "涨跌幅": 1.2,
                    }
                )
            ),
        ),
    ]
    for p in patchers:
        p.start()
    yield mock_llm, captured
    for p in patchers:
        p.stop()


@pytest.mark.asyncio
async def test_long_session_window_summary_and_delete(sqlite_checkpointer, chat_graph_patches):
    """>12 条消息（7 轮）：回答正常 + 12 条窗口 + 摘要注入 + messages_summary 持久化 + 删会话。"""
    saver = sqlite_checkpointer
    thread_id = uuid.uuid4().hex
    cfg = _thread_cfg(thread_id)

    graph = compile_chat_graph(checkpointer=saver)
    # 连跑 7 轮（每轮"600519 现在多少钱"均走 LLM 路径，已验证）：前 6 轮累积
    # 12 条历史（6H+6A），第 7 轮达 13 条（超窗）
    for _i in range(TOTAL_TURNS - 1):
        await graph.ainvoke(_base_state("600519 现在多少钱"), config=cfg)
    result = await graph.ainvoke(_base_state("600519 现在多少钱"), config=cfg)

    # 1a. 回答正常
    assert result["insight"] is not None
    assert result["final_response"].startswith("茅台当前 1800 元")

    # 1b. 第 7 轮 qa_router LLM 收到 12 条窗口（prompt 1 条 + 最近 12 条）
    _mock, captured = chat_graph_patches
    assert len(captured) == TOTAL_TURNS  # 每轮一次 qa LLM 调用
    llm_messages = captured[-1]  # 第 7 轮
    assert len(llm_messages) == 1 + 12
    prompt = llm_messages[0].content
    window = llm_messages[1:]
    # 窗口 = 最近 12 条（13 条中挤出最旧的 H1）：首条为 AI 回复（A1），
    # 末条为新问句（H7），H 消息共 6 条（H2..H7）
    assert isinstance(window[0].content, str)  # A1 文本
    assert isinstance(window[-1], HumanMessage)
    assert "600519 现在多少钱" in window[-1].content
    assert sum(isinstance(m, HumanMessage) for m in window) == 6

    # 1c. prompt 含"此前对话摘要"（超窗零 LLM 确定性摘要，含被挤出历史 H1）
    assert "此前对话摘要" in prompt
    assert "600519 现在多少钱" in prompt  # H1 收敛进摘要

    # 1d. checkpointer 持久化 messages_summary
    tup = await saver.aget_tuple(cfg)
    assert tup is not None
    summary = tup.checkpoint["channel_values"].get("messages_summary")
    assert summary is not None
    assert "600519 现在多少钱" in summary

    # 2. 删会话 → temp sqlite 无该 thread
    delete_thread(thread_id)
    assert (await saver.aget_tuple(cfg)) is None


@pytest.mark.asyncio
async def test_short_session_no_summary_byte_identical(sqlite_checkpointer, chat_graph_patches):
    """≤12 条消息（1 轮）：prompt 不含"此前对话摘要"，messages_summary 不持久化（零变化）。"""
    saver = sqlite_checkpointer
    thread_id = uuid.uuid4().hex
    cfg = _thread_cfg(thread_id)

    graph = compile_chat_graph(checkpointer=saver)
    result = await graph.ainvoke(_base_state("600519 现在多少钱"), config=cfg)

    # 回答正常（短会话主路径零变化）
    assert result["final_response"].startswith("茅台当前 1800 元")

    # 3. 短会话：qa 时 messages 仅 1 条（synth 的 AIMessage 在 qa 之后写入）→ prompt + 1 条窗口
    _mock, captured = chat_graph_patches
    assert len(captured) == 1
    llm_messages = captured[0]
    assert len(llm_messages) == 1 + 1  # prompt + 1 条
    prompt = llm_messages[0].content
    assert "此前对话摘要" not in prompt  # summary 空串 → prompt 字节不变

    # messages_summary 不持久化（短会话零变化硬约束）
    tup = await saver.aget_tuple(cfg)
    assert tup is not None
    assert "messages_summary" not in tup.checkpoint["channel_values"]
