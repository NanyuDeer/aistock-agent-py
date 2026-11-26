"""V4 公共契约的硬约束测试。

覆盖：必填时间边界、假设版本化、不可证伪假设拒绝、未知字段拒绝、
午报父判断强制、事件与观测分离。
"""

import pytest
from pydantic import ValidationError

from aistock_agent.brain_v4.contracts.briefing import BriefingEnvelope
from aistock_agent.brain_v4.contracts.events import CanonicalEventRecord, EventObservation
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest


def test_request_requires_as_of_and_snapshot():
    with pytest.raises(ValidationError) as exc:
        ResearchRequest(
            request_id="req-1",
            run_id="run-1",
            profile="market_trace",
            subject_type="market",
            subject_code="000001-SH",
            research_goal="解释今日下跌",
            horizon="1d",
        )
    message = str(exc.value)
    assert "as_of" in message
    assert "snapshot_id" in message


def test_hypothesis_revision_is_versioned():
    hypothesis = Hypothesis(
        hypothesis_id="hyp_liquidity",
        version=2,
        statement="流动性是放大器",
        status="weakened",
        prior_confidence=0.6,
        disconfirmers=["成长与防御同步下跌"],
        parent_ids=["hyp_liquidity:v1"],
        scope="market",
    )
    assert hypothesis.ref == "hyp_liquidity:v2"


def test_hypothesis_requires_disconfirmer():
    with pytest.raises(ValidationError):
        Hypothesis(
            hypothesis_id="hyp_x",
            version=1,
            statement="无失效条件的假设",
            status="open",
            prior_confidence=0.5,
            disconfirmers=[],
            scope="market",
        )


def test_contract_forbids_unknown_field():
    with pytest.raises(ValidationError):
        Hypothesis(
            hypothesis_id="hyp_y",
            version=1,
            statement="s",
            status="open",
            prior_confidence=0.5,
            disconfirmers=["d"],
            scope="market",
            unexpected_field=1,
        )


def test_midday_briefing_requires_parent_judgment():
    with pytest.raises(ValidationError):
        BriefingEnvelope(
            briefing_id="b-1",
            trading_date="2026-11-26",
            profile="briefing_midday",
            as_of="2026-11-26T11:35:00+08:00",
            data_cutoff="2026-11-26T11:30:00+08:00",
            run_id="run-mid",
            parent_judgment_ids=[],
            headline="午间",
            summary="汇总",
            body={
                "confirmed_hypothesis_refs": [],
                "falsified_hypothesis_refs": [],
                "new_drivers": [],
                "afternoon_opportunities": [],
                "afternoon_risks": [],
            },
            render_targets=["app"],
            schema_version="1.0",
        )


def test_event_record_separates_event_from_observations():
    observations = [
        EventObservation(
            observation_id=f"obs-{i}",
            event_id="ev-1",
            observation_type=kind,
            source=src,
            source_id=f"{src}-1",
            title="同一件事",
            published_at="2026-11-26T09:00:00+08:00",
            event_time="2026-11-26T08:50:00+08:00",
            fetched_at="2026-11-26T09:01:00+08:00",
            as_of="2026-11-26T10:00:00+08:00",
            time_confidence="high",
            lifecycle_version=1,
        )
        for i, (kind, src) in enumerate(
            [("news", "jin10"), ("announcement", "cninfo"), ("calendar", "tushare")]
        )
    ]
    event = CanonicalEventRecord(
        event_id="ev-1",
        canonical_type="policy",
        canonical_title="同一件事",
        event_time="2026-11-26T08:50:00+08:00",
        first_seen_at="2026-11-26T09:00:00+08:00",
        last_updated_at="2026-11-26T09:01:00+08:00",
        valid_time="2026-11-26T08:50:00+08:00",
        status="occurred",
        observation_ids=[o.observation_id for o in observations],
        dedupe_fingerprint="fp-1",
        time_confidence="high",
        lifecycle_version=1,
    )
    assert event.event_id == "ev-1"
    assert len(event.observation_ids) == 3
    assert not hasattr(event, "beneficiary_sector")
    assert not hasattr(event, "impact")
