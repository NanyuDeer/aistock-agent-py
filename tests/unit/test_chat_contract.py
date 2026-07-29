"""CHAT QA 链路数据契约校验测试。"""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from aistock_agent.schemas.chat_contract import (
    AnswerTrace,
    ChatSource,
    Evidence,
    Insight,
    InsightGoal,
    SkillCall,
)


def test_insight_goal_required_fields():
    with pytest.raises(ValidationError):
        InsightGoal()  # 缺 question / intent


def test_insight_goal_extra_forbidden():
    with pytest.raises(ValidationError):
        InsightGoal(
            question="茅台今天怎么样",
            intent="stock_snapshot",
            extra_field="not allowed",
        )


def test_insight_goal_answer_mode_optional():
    goal = InsightGoal(question="茅台今天", intent="stock_snapshot")
    assert goal.answer_mode is None


def test_insight_goal_intent_literal():
    with pytest.raises(ValidationError):
        InsightGoal(question="x", intent="invalid_intent")


def test_chat_source_required_fields():
    with pytest.raises(ValidationError):
        ChatSource()  # 缺 source_id / kind / title / snippet / captured_at


def test_chat_source_kind_literal():
    with pytest.raises(ValidationError):
        ChatSource(
            source_id="s1",
            kind="invalid_kind",
            title="t",
            snippet="s",
            captured_at=datetime.now(timezone.utc),
        )


def test_evidence_required_fields():
    with pytest.raises(ValidationError):
        Evidence()  # 缺 facts / sources / as_of / skill_name


def test_evidence_degraded_default_false():
    ev = Evidence(
        facts=["x"],
        sources=[],
        as_of=datetime.now(timezone.utc),
        skill_name="report_lookup",
    )
    assert ev.degraded is False
    assert ev.degraded_reason is None


def test_insight_required_fields():
    with pytest.raises(ValidationError):
        Insight()  # 缺 conclusion / basis / confidence / answer_mode


def test_insight_confidence_literal():
    with pytest.raises(ValidationError):
        Insight(
            conclusion="x",
            basis=[],
            confidence="0.85",  # 不接受数值
            answer_mode="validate",
        )


def test_skill_call_required_fields():
    with pytest.raises(ValidationError):
        SkillCall()  # 缺 skill_name / args


def test_skill_call_depends_on_default_empty():
    call = SkillCall(skill_name="report_lookup", args={})
    assert call.depends_on == []


def test_answer_trace_required_fields():
    with pytest.raises(ValidationError):
        AnswerTrace()  # 缺 goal / plan / skill_calls / evidences / actual_mode
