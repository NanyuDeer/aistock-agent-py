"""V4 结果 → 前端 `briefing.v1` 只读投影。

为什么单独一层：前端只消费固定信封，不能直接读 V4 内部契约；投影层把
`BriefingResult` 压缩成 app-api `BriefingEnvelope` 的字段，并保证：
- 顶层字段集合严格固定，新增字段必须先改前端契约；
- 只引用既有 claim / judgment / 证据，不新增任何 claim；
- `audio_path` 恒为 None（P3 不生成音频）；`evening_extras` 仅晚报有值；
- 不读取系统时间，`date` / `as_of` 全部来自 V4 结果与会话入参。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from aistock_agent.brain_v4.contracts.briefing import (
    BriefingProfile,
    EveningBody,
    MiddayBody,
    MorningBody,
)
from aistock_agent.brain_v4.features.briefing.projection import BriefingResult

_SCHEMA_VERSION = "briefing.v1"
_SLOT_BY_PROFILE: dict[BriefingProfile, str] = {
    "briefing_morning": "pre",
    "briefing_midday": "mid",
    "briefing_evening": "post",
}
_REPORT_TYPE_BY_PROFILE: dict[BriefingProfile, str] = {
    "briefing_morning": "morning",
    "briefing_midday": "midday",
    "briefing_evening": "evening",
}


@dataclass(frozen=True)
class ProjectionSession:
    """投影所需的会话边界；`evening_extras` 只由收盘行情构建，缺失即 None。"""

    profile: str
    trading_date: date
    evening_extras: dict[str, object] | None = None


def _section(
    *,
    section_id: str,
    title: str,
    conclusion: str,
    claim_ids: list[str],
    judgment_id: str | None,
    is_headline: bool,
    source: str | None = None,
    opportunities: list[str] | None = None,
    risks: list[str] | None = None,
) -> dict[str, object]:
    section: dict[str, object] = {
        "id": section_id,
        "title": title,
        "conclusion": conclusion,
        "source": source,
        "tags": [],
        "claim_ids": list(claim_ids),
        "judgment_id": judgment_id,
        "is_headline": is_headline,
    }
    if opportunities:
        section["opportunities"] = list(opportunities)
    if risks:
        section["risks"] = list(risks)
    return section


def _morning_sections(result: BriefingResult) -> list[dict[str, object]]:
    body = result.envelope.body
    if not isinstance(body, MorningBody):
        raise ValueError("briefing_morning 投影需要 MorningBody")
    claim_ids = [claim.claim_ref for claim in result.claims]
    sections = [
        _section(
            section_id="morning-headline",
            title="今日头条",
            conclusion=result.envelope.headline,
            claim_ids=claim_ids,
            judgment_id=result.judgment_id,
            is_headline=True,
        ),
        _section(
            section_id="morning-baseline",
            title="盘前基线",
            conclusion=body.today_baseline,
            claim_ids=claim_ids,
            judgment_id=result.judgment_id,
            is_headline=False,
        ),
    ]
    if body.overnight_changes:
        sections.append(
            _section(
                section_id="morning-overnight",
                title="隔夜变化",
                conclusion="；".join(body.overnight_changes),
                claim_ids=claim_ids,
                judgment_id=result.judgment_id,
                is_headline=False,
            )
        )
    if body.style_sector_hypotheses:
        sections.append(
            _section(
                section_id="morning-style-sector",
                title="风格与板块假设",
                conclusion="；".join(body.style_sector_hypotheses),
                claim_ids=claim_ids,
                judgment_id=result.judgment_id,
                is_headline=False,
            )
        )
    if body.scenarios:
        sections.append(
            _section(
                section_id="morning-scenarios",
                title="情景与失效条件",
                conclusion="；".join(
                    f"{scenario.label}：{scenario.expected_path}"
                    for scenario in body.scenarios
                ),
                claim_ids=claim_ids,
                judgment_id=result.judgment_id,
                is_headline=False,
                risks=list(body.invalidators),
            )
        )
    return sections


def _midday_sections(result: BriefingResult) -> list[dict[str, object]]:
    body = result.envelope.body
    if not isinstance(body, MiddayBody):
        raise ValueError("briefing_midday 投影需要 MiddayBody")
    carried_claims = [claim.claim_ref for claim in result.claims]
    sections = [
        _section(
            section_id="midday-headline",
            title="午间头条",
            conclusion=result.envelope.headline,
            claim_ids=carried_claims,
            judgment_id=result.judgment_id,
            is_headline=False,
        )
    ]
    if result.changes:
        sections.append(
            _section(
                section_id="midday-changes",
                title="判断修正",
                conclusion="；".join(
                    f"{change.claim_ref}：{change.relation}（{change.reason}）"
                    for change in result.changes
                ),
                claim_ids=[change.claim_ref for change in result.changes],
                judgment_id=result.judgment_id,
                is_headline=False,
            )
        )
    if body.new_drivers:
        sections.append(
            _section(
                section_id="midday-drivers",
                title="新增驱动",
                conclusion="；".join(body.new_drivers),
                claim_ids=carried_claims,
                judgment_id=result.judgment_id,
                is_headline=False,
            )
        )
    if body.afternoon_opportunities:
        sections.append(
            _section(
                section_id="midday-opportunities",
                title="午后机会",
                conclusion="；".join(body.afternoon_opportunities),
                claim_ids=carried_claims,
                judgment_id=result.judgment_id,
                is_headline=False,
                opportunities=list(body.afternoon_opportunities),
            )
        )
    if body.afternoon_risks:
        sections.append(
            _section(
                section_id="midday-risks",
                title="午后风险",
                conclusion="；".join(body.afternoon_risks),
                claim_ids=carried_claims,
                judgment_id=result.judgment_id,
                is_headline=False,
                risks=list(body.afternoon_risks),
            )
        )
    return sections


def _evening_sections(result: BriefingResult) -> list[dict[str, object]]:
    body = result.envelope.body
    if not isinstance(body, EveningBody):
        raise ValueError("briefing_evening 投影需要 EveningBody")
    scored_claims = [score.claim_ref for score in result.scores]
    sections = [
        _section(
            section_id="evening-close-review",
            title="收盘复盘",
            conclusion=result.envelope.summary,
            claim_ids=scored_claims,
            judgment_id=result.judgment_id,
            is_headline=False,
        )
    ]
    if body.prediction_scorecard:
        sections.append(
            _section(
                section_id="evening-scorecard",
                title="预测评分",
                conclusion="；".join(body.prediction_scorecard),
                claim_ids=scored_claims,
                judgment_id=result.market_trace_judgment_id,
                is_headline=False,
            )
        )
    # 晚报唯一的 claim 槽位：盘后复核结论（分析师可覆盖，不改写早/午报 claim）。
    statements = [
        claim.statement for claim in result.claims if claim.statement.strip()
    ]
    if statements:
        sections.append(
            _section(
                section_id="evening-claims",
                title="盘后复核",
                conclusion="；".join(statements),
                claim_ids=[claim.claim_ref for claim in result.claims],
                judgment_id=result.judgment_id,
                is_headline=False,
            )
        )
    if body.errors_and_unresolved:
        sections.append(
            _section(
                section_id="evening-errors",
                title="错误与未决",
                conclusion="；".join(body.errors_and_unresolved),
                claim_ids=scored_claims,
                judgment_id=result.judgment_id,
                is_headline=False,
            )
        )
    if body.next_day_verification:
        sections.append(
            _section(
                section_id="evening-next-day",
                title="次日验证",
                conclusion="；".join(body.next_day_verification),
                claim_ids=scored_claims,
                judgment_id=result.judgment_id,
                is_headline=False,
            )
        )
    return sections


def _judgment_ids(result: BriefingResult) -> list[str]:
    ids = [result.judgment_id]
    market_trace_id = result.market_trace_judgment_id
    if market_trace_id and market_trace_id not in ids:
        ids.append(market_trace_id)
    return ids


def to_briefing_v1(
    result: BriefingResult, session: ProjectionSession
) -> dict[str, object]:
    """把单时段 V4 结果投影为前端 `briefing.v1`。

    `session.profile` 必须与结果信封一致：错配会导致 slot/report_type 与内容
    不符，直接报错而不是静默产出错误前端结构。
    """
    profile = result.envelope.profile
    if session.profile != profile:
        raise ValueError(
            f"投影会话 profile={session.profile!r} 与结果 {profile!r} 不一致"
        )
    if profile == "briefing_morning":
        sections = _morning_sections(result)
        highlight_source = next(
            (section for section in sections if section["is_headline"]), None
        )
        highlight = (
            None
            if highlight_source is None
            else {
                "label": "今日头条",
                "title": highlight_source["title"],
                "conclusion": highlight_source["conclusion"],
                "tags": [],
            }
        )
    elif profile == "briefing_midday":
        sections = _midday_sections(result)
        highlight = None
    else:
        sections = _evening_sections(result)
        highlight = None

    return {
        "schema_version": _SCHEMA_VERSION,
        "report_type": _REPORT_TYPE_BY_PROFILE[profile],
        "slot": _SLOT_BY_PROFILE[profile],
        "date": session.trading_date.isoformat(),
        "as_of": result.envelope.as_of.isoformat(),
        "display_report": {
            "summary": result.envelope.summary,
            "highlight": highlight,
            "sections": sections,
            "risks": list(result.envelope.risk_flags),
        },
        "podcast_brief": result.envelope.summary,
        "audio_path": None,
        "judgment_ids": _judgment_ids(result),
        "evening_extras": (
            session.evening_extras if profile == "briefing_evening" else None
        ),
    }
