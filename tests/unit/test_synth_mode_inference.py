"""synth_answer 模式推断规则单元测试。"""
from datetime import datetime, timezone

from aistock_agent.graph.nodes.synth_answer import _infer_answer_mode
from aistock_agent.schemas.chat_contract import Evidence, InsightGoal


def _goal(intent: str = "report_lookup", time_range="today", constraints=None) -> InsightGoal:
    return InsightGoal(
        question="x",
        intent=intent,
        time_range=time_range,
        constraints=constraints or {},
    )


def _evidence(degraded: bool = False) -> Evidence:
    return Evidence(
        facts=["x"],
        sources=[],
        as_of=datetime.now(timezone.utc),
        degraded=degraded,
        skill_name="test",
    )


def test_infer_default_report_lookup_validate():
    assert _infer_answer_mode(_goal("report_lookup"), [_evidence()]) == "validate"


def test_infer_default_stock_snapshot_validate():
    assert _infer_answer_mode(_goal("stock_snapshot"), [_evidence()]) == "validate"


def test_infer_default_stock_news_trace():
    assert _infer_answer_mode(_goal("stock_news"), [_evidence()]) == "trace"


def test_infer_default_trace_lookup_trace():
    assert _infer_answer_mode(_goal("trace_lookup"), [_evidence()]) == "trace"


def test_infer_default_industry_relation_trace():
    assert _infer_answer_mode(_goal("industry_relation"), [_evidence()]) == "trace"


def test_infer_constraints_override():
    """constraints.answer_mode 显式覆盖优先。"""
    goal = _goal("report_lookup", constraints={"answer_mode": "predict"})
    assert _infer_answer_mode(goal, [_evidence()]) == "predict"


def test_infer_degraded_forces_validate():
    """任意 Evidence degraded → 强制 validate。"""
    goal = _goal("trace_lookup")  # 默认 trace
    assert _infer_answer_mode(goal, [_evidence(degraded=True)]) == "validate"


def test_infer_realtime_skips_validate():
    """time_range=realtime + 默认 validate → 改为 trace。"""
    goal = _goal("stock_snapshot", time_range="realtime")
    assert _infer_answer_mode(goal, [_evidence()]) == "trace"


def test_infer_degraded_overrides_realtime():
    """degraded 优先级高于 realtime 修正。"""
    goal = _goal("stock_snapshot", time_range="realtime")
    assert _infer_answer_mode(goal, [_evidence(degraded=True)]) == "validate"
