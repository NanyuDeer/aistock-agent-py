"""前端 `briefing.v1` 投影单测：字段严格匹配、投影不新增 claim。

这里直接构造已由 V4 链产出的 `BriefingResult`，只验证投影层：
- 顶层字段集合与 app-api `BriefingEnvelope` 完全一致；
- 早/午/晚的 slot/report_type 映射正确；
- `display_report` 的 sections 只引用已有 claim 与 judgment，不新增 claim；
- `audio_path` 恒为 None，`evening_extras` 仅 post 有值。
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from aistock_agent.brain_v4.contracts.briefing import (
    BriefingEnvelope,
    EveningBody,
    MorningBody,
    Scenario,
)
from aistock_agent.brain_v4.features.briefing.projection import (
    BriefingClaim,
    BriefingResult,
    ClaimScore,
)
from aistock_agent.brain_v4.runtime.briefing_projection import (
    ProjectionSession,
    to_briefing_v1,
)

CST = timezone(timedelta(hours=8))
AS_OF = datetime(2026, 11, 27, 8, 30, tzinfo=CST)
TRADING_DATE = date(2026, 11, 27)
TOP_LEVEL_KEYS = {
    "schema_version",
    "report_type",
    "slot",
    "date",
    "as_of",
    "display_report",
    "podcast_brief",
    "audio_path",
    "judgment_ids",
    "evening_extras",
}


def _morning_result() -> BriefingResult:
    body = MorningBody(
        overnight_changes=["隔夜海外风险资产走弱"],
        today_baseline="指数低开概率较大，关注成交额与风格分化。",
        key_event_ids=["ev_brief_a"],
        style_sector_hypotheses=["风格分化：成长弱于防御"],
        scenarios=[
            Scenario(
                scenario_id="scn_base",
                label="基准情景",
                trigger_conditions=["成交额温和"],
                expected_path="指数窄幅震荡",
                invalidator="若成交额显著放大则本情景失效",
            )
        ],
        invalidators=["若成交额显著放大，则流动性收缩假设失效"],
    )
    envelope = BriefingEnvelope(
        briefing_id="brief_morning",
        trading_date=TRADING_DATE,
        profile="briefing_morning",
        as_of=AS_OF,
        data_cutoff=AS_OF,
        run_id="run_morning",
        headline="早报：隔夜风险偏好回落",
        summary="隔夜风险资产走弱，盘前关注风格分化。",
        evidence_ids=["ev_brief_a"],
        risk_flags=["隔夜外部风险"],
        body=body,
        render_targets=["app", "web", "text", "audio"],
        schema_version="v1",
    )
    return BriefingResult(
        envelope=envelope,
        judgment_id="jdg_run_morning",
        direction="down",
        confidence=0.62,
        risk_level="medium",
        claims=[
            BriefingClaim(
                claim_ref="hyp_morning_style",
                source_profile="briefing_morning",
                statement="风格分化导致成长领跌",
                material=True,
                confidence=0.45,
            ),
            BriefingClaim(
                claim_ref="hyp_morning_liquidity",
                source_profile="briefing_morning",
                statement="流动性收缩是今日下跌主因",
                material=True,
                confidence=0.55,
            ),
        ],
    )


def _evening_result() -> BriefingResult:
    body = EveningBody(
        closing_attribution_ref="market_trace:jdg_market_trace",
        market_trace_judgment_id="jdg_market_trace",
        prediction_scorecard=[
            "hyp_morning_style|confirmed|0.72|jdg_market_trace"
        ],
        errors_and_unresolved=["板块轮动是否独立仍待验证"],
    )
    envelope = BriefingEnvelope(
        briefing_id="brief_evening",
        trading_date=TRADING_DATE,
        profile="briefing_evening",
        as_of=AS_OF,
        data_cutoff=AS_OF,
        run_id="run_evening",
        parent_run_id="run_midday",
        parent_judgment_ids=["jdg_run_morning", "jdg_run_midday"],
        relation="verify",
        headline="晚报：收盘风格分化主导",
        summary="收盘指数下跌，晚报引用市场溯源研判。",
        evidence_ids=["ev_brief_close"],
        risk_flags=["隔夜外部风险"],
        body=body,
        render_targets=["app", "web", "text", "audio"],
        schema_version="v1",
    )
    return BriefingResult(
        envelope=envelope,
        judgment_id="jdg_run_evening",
        direction="down",
        confidence=0.66,
        risk_level="high",
        scores=[
            ClaimScore(
                claim_ref="hyp_morning_style",
                source_profile="briefing_morning",
                verdict="confirmed",
                score=0.72,
                market_trace_judgment_id="jdg_market_trace",
            )
        ],
        market_trace_judgment_id="jdg_market_trace",
    )


def test_morning_projection_matches_briefing_v1_contract() -> None:
    session = ProjectionSession(
        profile="briefing_morning", trading_date=TRADING_DATE
    )
    output = to_briefing_v1(_morning_result(), session)

    assert set(output) == TOP_LEVEL_KEYS
    assert output["schema_version"] == "briefing.v1"
    assert output["report_type"] == "morning"
    assert output["slot"] == "pre"
    assert output["date"] == "2026-11-27"
    assert output["as_of"] == AS_OF.isoformat()
    assert output["audio_path"] is None
    assert output["judgment_ids"] == ["jdg_run_morning"]
    assert output["evening_extras"] is None

    display = output["display_report"]
    assert isinstance(display, dict)
    assert set(display) == {"summary", "highlight", "sections", "risks"}
    assert display["summary"]
    assert display["highlight"] is not None
    assert display["risks"] == ["隔夜外部风险"]

    sections = display["sections"]
    assert isinstance(sections, list) and sections
    for section in sections:
        assert isinstance(section, dict)
        assert {
            "id",
            "title",
            "conclusion",
            "source",
            "tags",
            "claim_ids",
            "judgment_id",
            "is_headline",
        } <= set(section)
        assert isinstance(section["tags"], list)
        # 投影只允许引用已有 claim / judgment，不得新增 claim。
        assert set(section["claim_ids"]) <= {
            "hyp_morning_style",
            "hyp_morning_liquidity",
        }
        assert section["judgment_id"] in {None, "jdg_run_morning"}

    headline = next(section for section in sections if section["is_headline"])
    assert set(headline["claim_ids"]) == {
        "hyp_morning_style",
        "hyp_morning_liquidity",
    }


def test_evening_projection_only_post_has_evening_extras_and_audio_is_null() -> None:
    extras = {
        "has_anomaly": True,
        "anomaly_conclusion": "指数下跌且成长弱于防御",
        "indexes": [
            {"name": "上证指数", "close": 3012.4, "pct_change": -1.18}
        ],
        "breadth": {
            "advance_count": 1800,
            "decline_count": 3200,
            "flat_count": 200,
        },
        "sector_ranking": {
            "top_gainers": [{"name": "银行", "pct_change": 0.9}],
            "top_losers": [{"name": "半导体", "pct_change": -2.2}],
        },
    }
    session = ProjectionSession(
        profile="briefing_evening",
        trading_date=TRADING_DATE,
        evening_extras=extras,
    )
    output = to_briefing_v1(_evening_result(), session)

    assert set(output) == TOP_LEVEL_KEYS
    assert output["report_type"] == "evening"
    assert output["slot"] == "post"
    assert output["audio_path"] is None
    assert output["evening_extras"] == extras
    assert output["judgment_ids"] == ["jdg_run_evening", "jdg_market_trace"]

    display = output["display_report"]
    assert isinstance(display, dict)
    assert display["highlight"] is None
    sections = display["sections"]
    assert isinstance(sections, list) and sections
    claim_ids = {
        claim_id
        for section in sections
        if isinstance(section, dict)
        for claim_id in section["claim_ids"]
    }
    # 晚报只引用早报既有 claim，不新增。
    assert claim_ids <= {"hyp_morning_style"}
