"""synth_answer 返回 dict 键契约（G1 守卫，2026-08-17）。

契约：deep 分支写 last_deep_report；7 个非 deep 返回点不写该键（LastValue 通道
不被覆盖 → checkpoint 跨轮引用保持 → T5 追问注入零影响）。

实现说明（裁决 #7 替代方案）：本文件不 mock graph 驱动 synth_answer 全分支——
各非 deep 分支依赖 goal/evidences 完整状态与 LLM 调用，直接单测成本高；且 light
分支走真实 LLM 调用会触发外部网络（测试环境禁止）。契约语义改为两层覆盖：
1. 本文件：deep 分支构建 DeepReportRef 非 None 的最小单测（_build_deep_report_ref
   纯函数，不触发 LLM）——锁 deep 分支"写入非 None 引用"；
2. tests/integration/test_chat_stream_checkpoint.py（层 3）：真实
   compile_chat_graph + MemorySaver 两轮（deep→light）+ graph.aget_state 保留
   非 None——锁"非 deep 轮不写键 → LastValue 通道不被覆盖"。

禁止未来"state 层显式 None 化"破坏该契约（硬约束 #1）：deep 分支必须返回
last_deep_report 键且值非 None，7 个非 deep 返回点不得返回该键。
"""
from unittest.mock import patch

import pytest

from aistock_agent.graph.nodes.synth_answer import (
    SynthInsightOutput,
    _build_deep_report_ref,
    synth_answer_node,
)
from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.state.chat_schema import DeepReportRef, QuestionState


def test_deep_branch_build_deep_report_ref_non_none():
    """G1 契约：deep 分支用 _build_deep_report_ref 构建非 None 引用（不触发 LLM）。

    _build_deep_report_ref 是 synth_answer deep 分支唯一构造 last_deep_report 的
    纯函数入口（report_id 未登录为 None，其余字段透传；worker 由 deep_source 保证合法）。
    若未来 deep 分支改为返回 None / 缺键（state 层显式 None 化），本单测不再调用
    该构造点 → 改为引用不存在/返回 None → 测试编译或断言失败，即红。
    """
    ref: DeepReportRef | None = _build_deep_report_ref(
        worker="stock",
        question="深度分析贵州茅台",
        final_response="深度分析全文（测试用）",
        symbols=["600519"],
        tag_codes=[],
        report_id=None,
        created_at="2026-08-17T00:00:00+00:00",
    )

    assert ref is not None
    assert ref["worker"] == "stock"
    assert ref["report_id"] is None
    assert ref["question"] == "深度分析贵州茅台"
    assert ref["summary"] == "深度分析全文（测试用）"[:160]
    assert ref["symbols"] == ["600519"]
    assert ref["tag_codes"] == []
    assert ref["created_at"] == "2026-08-17T00:00:00+00:00"


def test_deep_branch_ref_keeps_report_id_when_persisted():
    """G1 契约：落库成功时 report_id 回填非 None（D39：引用与登录解耦、report_id 透传）。"""
    ref = _build_deep_report_ref(
        worker="stock",
        question="深度分析贵州茅台",
        final_response="深度分析全文",
        symbols=["600519"],
        tag_codes=[],
        report_id="rep_persisted_1",
        created_at="2026-08-17T00:00:00+00:00",
    )

    assert ref is not None
    assert ref["report_id"] == "rep_persisted_1"


def test_deep_branch_build_deep_report_ref_is_dict_with_expected_keys():
    """G1 契约：DeepReportRef 是 TypedDict，构造结果必须是 dict 且含全部契约键。

    防止未来把 DeepReportRef 替换为 None/裸串/缺键结构（破坏 SSE/HTTP 透出契约）。
    """
    ref = _build_deep_report_ref(
        worker="stock",
        question="q",
        final_response="r",
        symbols=["600519"],
        tag_codes=[],
        report_id=None,
        created_at="2026-08-17T00:00:00+00:00",
    )

    assert isinstance(ref, dict)
    for key in ("worker", "report_id", "question", "summary", "symbols", "tag_codes", "created_at"):
        assert key in ref


def test_synth_insight_output_questions_field():
    """SynthInsightOutput 必须含可选 questions 字段（默认空列表）。"""
    out = SynthInsightOutput(
        conclusion="## 核心结论\n短期震荡。\n## 行情要点\n- 指数收涨 0.5%",
        basis_indices=[1],
        confidence="low",
        uncertainty=[],
        answer_mode="validate",
    )
    assert out.questions == []


def test_synth_insight_output_questions_roundtrip():
    """LLM 提供 questions 时完整透传（2-4 条问句）。"""
    out = SynthInsightOutput(
        conclusion="## 核心结论\n短期震荡。",
        basis_indices=[],
        confidence="low",
        uncertainty=[],
        answer_mode="validate",
        questions=["今天大盘成交量如何？", "哪些板块领涨？"],
    )
    assert len(out.questions) == 2


def _minimal_state(**extra) -> QuestionState:
    """构造最小 QuestionState：goal 缺省为 None（命中无 goal 分支）。"""
    state: QuestionState = {"messages": [], "user_id": None}
    state.update(extra)
    return state


@pytest.mark.asyncio
async def test_clarification_branch_insight_has_empty_questions():
    """澄清分支（qa_router 兜底缺失个股代码）Insight.questions 恒空（面板不升级）。

    注意：澄清分支在 goal 缺失检查之后，state 必须带合法 goal 才会走到该分支；
    仅带 clarification 会命中无 goal 分支（由 test_no_goal_branch 覆盖）。
    """
    state = _minimal_state(
        goal=InsightGoal(question="分析贵州茅台", intent="stock_snapshot"),
        clarification="请提供 6 位股票代码",
    )
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("澄清分支不应调用 LLM"),
    ):
        result = await synth_answer_node(state)
    assert result["insight"].questions == []


@pytest.mark.asyncio
async def test_gateway_shortcut_branch_insight_has_empty_questions():
    """闸门短路分支（final_response 话术直出）Insight.questions 恒空（面板不升级）。"""
    state = _minimal_state(
        goal=InsightGoal(question="你是谁", intent="stock_snapshot"),
        final_response="我是 AI 投资助手，可以为您分析个股、板块与大盘。",
    )
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("闸门短路分支不应调用 LLM"),
    ):
        result = await synth_answer_node(state)
    assert result["insight"].questions == []


@pytest.mark.asyncio
async def test_no_goal_branch_insight_has_empty_questions():
    """无 goal 降级分支（_build_degraded_insight）Insight.questions 恒空（面板不升级）。"""
    with patch(
        "aistock_agent.graph.nodes.synth_answer.get_deep_think",
        side_effect=AssertionError("无 goal 分支不应调用 LLM"),
    ):
        result = await synth_answer_node(_minimal_state())
    assert result["insight"].questions == []
