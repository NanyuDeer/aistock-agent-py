"""分析师任务文本必须讲清“证据是什么”，而不是只给 ID。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aistock_agent.brain_v4.analysts.context import MAX_TASK_CHARS, build_analyst_task
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem
from aistock_agent.brain_v4.contracts.research import Hypothesis

CST = timezone(timedelta(hours=8))


def _hypothesis(ref: str, statement: str) -> Hypothesis:
    return Hypothesis(
        hypothesis_id=ref.split(":")[0],
        version=1,
        statement=statement,
        status="open",
        prior_confidence=0.55,
        disconfirmers=["若成交额回升则证伪"],
        scope="market",
    )


def _evidence(evidence_id: str, source: str, content_time: datetime) -> EvidenceItem:
    return EvidenceItem(
        evidence_id=evidence_id,
        source=source,
        source_type="market_fact",
        event_time=content_time,
        published_at=content_time,
        fetched_at=content_time,
        as_of=content_time,
        evidence_kind="direct",
        data_quality="high",
        time_confidence="high",
        tool="close_snapshot",
        tool_params={"date": "20261126"},
        response_digest="digest",
        snapshot_id="snap",
    )


def test_task_contains_hypothesis_statements() -> None:
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal="解释缩量下跌",
        hypotheses=[
            _hypothesis("hyp_liquidity:v1", "流动性收缩是根因"),
            _hypothesis("hyp_style:v1", "风格切换是根因"),
        ],
        evidence=[],
    )
    assert "流动性收缩是根因" in task
    assert "风格切换是根因" in task
    assert "hyp_liquidity:v1" in task


def test_task_contains_evidence_content() -> None:
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=[_evidence("ev_1", "INDEX_000300_SH", now)],
        sources={"INDEX_000300_SH": "沪深300 3900.55 +0.59%"},
    )
    assert "沪深300 3900.55 +0.59%" in task
    assert "ev_1" in task


def test_task_falls_back_to_tool_params_without_sources() -> None:
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=[_evidence("ev_1", "INDEX_000300_SH", now)],
    )
    assert "date=20261126" in task


def test_task_is_bounded() -> None:
    now = datetime(2026, 11, 26, 15, 5, tzinfo=CST)
    evidence = [
        _evidence(f"ev_{index}", f"SRC_{index}", now) for index in range(60)
    ]
    sources = {f"SRC_{index}": "内容" * 500 for index in range(60)}
    task = build_analyst_task(
        mode="SOLO_DEEP",
        role="lead",
        round_no=0,
        subject="000001",
        goal=None,
        hypotheses=[_hypothesis("hyp_liquidity:v1", "流动性收缩")],
        evidence=evidence,
        sources=sources,
    )
    assert len(task) <= MAX_TASK_CHARS
    # 假设永远优先保留，不被证据挤掉。
    assert "流动性收缩" in task

