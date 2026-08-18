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
import pytest

from aistock_agent.graph.nodes.synth_answer import _build_deep_report_ref
from aistock_agent.state.chat_schema import DeepReportRef


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
