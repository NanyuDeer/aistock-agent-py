"""早午晚报日内链试点集成测试（V4-09-P01 / Task 9）。

覆盖：
- 早报 cutoff 隔离：cutoff 之后发布的重要消息、event_time 之前不可见的事件
  都不得进入早报证据；
- 早报保留可证伪情景与失效条件；
- 三个时段使用独立 run/snapshot/cutoff/ledger namespace，`as_of` 全部来自 fixture；
- 午报复用未变化证据，只对受影响假设增量修正，每个变化带 parent + relation；
- 晚报按 ID 引用 Task 8 市场溯源研判并对早/午报 claim 打分，不另起盘后归因；
- 评测信封的 parent chain 与双栏报告真实暴露市场溯源 judgment id。

全部测试离线、确定性、不读系统时间、不触网、不调用真实 LLM。
"""

from __future__ import annotations

import datetime
from pathlib import Path

from aistock_agent.brain_v4.contracts.briefing import EveningBody, MiddayBody
from aistock_agent.brain_v4.features.briefing.adapter import (
    BriefingChainFixture,
    EvidenceWindow,
    build_chain_windows,
    build_evidence_window,
    load_briefing_chain,
)
from aistock_agent.brain_v4.features.briefing.evaluation import (
    build_briefing_envelope,
    generate_briefing_reports,
    market_trace_reference,
)
from aistock_agent.brain_v4.features.briefing.projection import (
    project_chain,
    project_midday,
    project_morning,
)
from aistock_agent.brain_v4.features.market_trace.adapter import load_frozen_snapshot
from aistock_agent.brain_v4.features.market_trace.profile import run_market_trace
from aistock_agent.brain_v4.graph.profiles import ledger_namespace

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "brain_v4"
    / "briefing"
    / "day_chain.json"
)
_FEATURE_DIR = (
    _REPO_ROOT
    / "src"
    / "aistock_agent"
    / "brain_v4"
    / "features"
    / "briefing"
)
_BANNED_SOURCE_TOKENS = (
    "datetime.now(",
    "datetime.utcnow(",
    "date.today(",
    "create_react_agent",
    "requests.",
    "httpx.",
)


def _chain() -> BriefingChainFixture:
    return load_briefing_chain(_FIXTURE)


def _source_ids(window: EvidenceWindow) -> set[str]:
    return {str(item.tool_params["source_id"]) for item in window.items}


def _task8_market_trace(chain: BriefingChainFixture) -> tuple[object, object]:
    case = load_frozen_snapshot(_REPO_ROOT / chain.market_trace.case_fixture)
    run = run_market_trace(case)
    return market_trace_reference(run), run


def test_morning_cutoff_isolates_post_cutoff_and_future_events() -> None:
    chain = _chain()
    morning = build_evidence_window(chain, "briefing_morning")
    midday = build_evidence_window(chain, "briefing_midday")

    morning_sources = _source_ids(morning)
    midday_sources = _source_ids(midday)

    # fixture 中两条重要消息的 published_at 晚于早报 cutoff。
    published_after_cutoff = {
        item.source_id: item
        for item in chain.items
        if item.published_at > morning.data_cutoff
    }
    assert {"AM_STYLE_CONFIRM", "AM_TURNOVER_CONFIRM"} <= set(published_after_cutoff)

    # 早报不得看到 cutoff 之后发布的消息。
    assert "AM_STYLE_CONFIRM" not in morning_sources
    assert "AM_TURNOVER_CONFIRM" not in morning_sources

    # 同日事件在 event_time 之前不可见。
    assert "INTRADAY_BACKDATED" not in morning_sources
    assert "INTRADAY_BACKDATED" not in midday_sources

    # 早报仍保留盘前可见证据。
    assert {
        "OVERNIGHT_GLOBAL_RISK",
        "MORNING_POLICY",
        "CALENDAR_MORNING",
    } <= morning_sources

    for item in morning.items:
        assert item.event_time <= morning.data_cutoff
        assert item.published_at <= morning.as_of
        assert item.as_of == morning.as_of
        assert item.snapshot_id == morning.snapshot_id

    for item in midday.items:
        assert item.event_time <= midday.data_cutoff

    # 午报能看到上午确认/否定证据，但仍看不到午后事件。
    assert {"AM_STYLE_CONFIRM", "AM_TURNOVER_CONFIRM"} <= midday_sources


def test_morning_keeps_falsifiable_scenario_and_invalidator() -> None:
    chain = _chain()
    morning = project_morning(chain, build_evidence_window(chain, "briefing_morning"))

    body = morning.envelope.body
    assert body.scenarios
    for scenario in body.scenarios:
        assert scenario.trigger_conditions
        assert scenario.expected_path
        assert scenario.invalidator
    assert body.invalidators
    assert morning.envelope.evidence_ids
    assert set(body.key_event_ids) <= set(morning.envelope.evidence_ids)


def test_three_sessions_use_distinct_run_snapshot_cutoff_and_ledger_namespace() -> None:
    chain = _chain()
    windows = build_chain_windows(chain)

    assert set(windows) == {
        "briefing_morning",
        "briefing_midday",
        "briefing_evening",
    }
    assert len({window.run_id for window in windows.values()}) == 3
    assert len({window.snapshot_id for window in windows.values()}) == 3
    assert len({window.data_cutoff for window in windows.values()}) == 3

    for profile, window in windows.items():
        assert window.ledger_namespace == ledger_namespace(profile, window.run_id)
        assert window.ledger_namespace.startswith(f"{profile}:")

    sessions = {session.profile: session for session in chain.sessions}
    for profile, window in windows.items():
        assert window.as_of == sessions[profile].as_of
        assert window.data_cutoff == sessions[profile].data_cutoff


def test_midday_reuses_evidence_and_reopens_only_impacted_claims() -> None:
    chain = _chain()
    windows = build_chain_windows(chain)
    morning = project_morning(chain, windows["briefing_morning"])
    midday = project_midday(chain, windows["briefing_midday"], morning)

    # 未变化证据按同一 evidence ID 直接复用。
    assert morning.envelope.evidence_ids
    assert set(morning.envelope.evidence_ids) <= set(midday.envelope.evidence_ids)

    # 午报必须携带父判断。
    assert midday.envelope.parent_judgment_ids == [morning.judgment_id]
    assert midday.envelope.parent_run_id == morning.envelope.run_id

    changes = {change.claim_ref: change for change in midday.changes}
    assert set(changes) == {"hyp_morning_style", "hyp_morning_liquidity"}
    assert changes["hyp_morning_style"].relation == "confirm"
    assert changes["hyp_morning_liquidity"].relation == "reverse"
    for change in midday.changes:
        assert change.parent_judgment_id == morning.judgment_id
        assert change.evidence_ids
        assert set(change.evidence_ids) <= set(midday.envelope.evidence_ids)

    # 未受影响假设保持原状，不重写。
    assert "hyp_morning_sector" not in changes
    assert {claim.claim_ref for claim in midday.claims} == {
        claim.claim_ref for claim in morning.claims
    }
    assert "hyp_morning_sector" in {claim.claim_ref for claim in midday.claims}

    body = midday.envelope.body
    assert isinstance(body, MiddayBody)
    assert body.confirmed_hypothesis_refs == ["hyp_morning_style"]
    assert body.falsified_hypothesis_refs == ["hyp_morning_liquidity"]


def test_evening_references_task8_market_trace_and_scores_every_material_claim() -> None:
    chain = _chain()
    reference, market_run = _task8_market_trace(chain)
    result = project_chain(chain, reference)

    judgment_id = getattr(market_run, "judgment").judgment_id
    body = result.evening.envelope.body
    assert isinstance(body, EveningBody)
    assert body.market_trace_judgment_id == judgment_id
    assert result.evening.market_trace_judgment_id == judgment_id
    assert body.closing_attribution_ref.endswith(judgment_id)
    assert judgment_id.startswith("jdg_")

    # parent chain：晚报同时携带早报与午报判断。
    assert result.evening.envelope.parent_judgment_ids == [
        result.morning.judgment_id,
        result.midday.judgment_id,
    ]
    assert result.evening.envelope.parent_run_id == result.midday.envelope.run_id

    # 晚报不产生第二套主因判断：自身无 claim，只按 ID 引用市场溯源。
    assert result.evening.claims == []
    assert result.evening.judgment_id != judgment_id

    material_claims = {
        hypothesis.claim_ref for hypothesis in chain.hypotheses if hypothesis.material
    }
    assert material_claims <= {score.claim_ref for score in result.evening.scores}
    scorecard = " ".join(body.prediction_scorecard)
    for score in result.evening.scores:
        assert score.market_trace_judgment_id == judgment_id
        assert score.claim_ref in scorecard
        assert judgment_id in scorecard


def test_evaluation_parent_chain_and_report_exposes_market_trace(tmp_path: Path) -> None:
    chain = _chain()
    reference, market_run = _task8_market_trace(chain)
    result = project_chain(chain, reference)
    judgment_id = getattr(market_run, "judgment").judgment_id

    envelopes = [
        build_briefing_envelope(chain, result.morning),
        build_briefing_envelope(chain, result.midday),
        build_briefing_envelope(chain, result.evening),
    ]
    assert [envelope.profile for envelope in envelopes] == [
        "briefing_morning",
        "briefing_midday",
        "briefing_evening",
    ]
    assert envelopes[1].v4_output["parent_judgment_ids"] == [
        result.morning.judgment_id
    ]
    assert envelopes[2].v4_output["parent_judgment_ids"] == [
        result.morning.judgment_id,
        result.midday.judgment_id,
    ]
    assert envelopes[2].v4_output["market_trace_judgment_id"] == judgment_id
    assert envelopes[1].legacy_run_id != envelopes[1].v4_run_id

    paths = generate_briefing_reports(
        chain,
        [result.morning, result.midday, result.evening],
        tmp_path / "report",
    )
    html = paths.pages["briefing"].read_text(encoding="utf-8")
    assert "旧链 Legacy" in html
    assert "V4" in html
    assert result.morning.judgment_id in html
    assert result.midday.judgment_id in html
    assert judgment_id in html


def test_briefing_feature_is_offline_and_time_explicit() -> None:
    sources = [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(_FEATURE_DIR.rglob("*.py"))
    ]
    assert sources, "briefing feature 源码目录为空，无法执行离线/时间纪律扫描"
    offenders = [
        f"{path.name}:{token}"
        for path, text in sources
        for token in _BANNED_SOURCE_TOKENS
        if token in text
    ]
    assert offenders == []


def test_fixture_times_are_frozen_constants_not_system_clock() -> None:
    chain = _chain()
    cutoff = chain.sessions[0].data_cutoff
    assert cutoff == datetime.datetime(
        2026, 11, 26, 8, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=8))
    )
