"""早午晚报跨通道投影：App / Web / text / audio。

硬约束（计划 Task 9 Step 4 / 团队接入规范 §6 Renderer 角色）：
- 四种通道共享同一 headline、direction、relation、risk level、evidence IDs 与
  confidence，任何通道都不得改写这些字段；
- audio 只允许缩短措辞，不得新增 claim；
- 本模块只做展示投影，不含研究逻辑，也不调用任何工具。
"""

from __future__ import annotations

from typing import Literal, cast

from pydantic import Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.briefing import (
    BriefingRelation,
    EveningBody,
    MiddayBody,
    MorningBody,
)
from aistock_agent.brain_v4.features.briefing.adapter import MarketDirection, RiskLevel
from aistock_agent.brain_v4.features.briefing.projection import BriefingResult

Channel = Literal["app", "web", "text", "audio"]
_CHANNELS: tuple[Channel, ...] = ("app", "web", "text", "audio")


class RenderedBriefing(V4Contract):
    """单通道投影结果；共享字段必须与简报信封一致。"""

    briefing_id: str
    channel: Channel
    headline: str
    direction: MarketDirection
    relation: BriefingRelation | None
    risk_level: RiskLevel
    confidence: float = Field(ge=0.0, le=1.0)
    evidence_ids: list[str] = Field(default_factory=list)
    claim_refs: list[str] = Field(default_factory=list)
    body_text: str


def _claim_refs(briefing: BriefingResult) -> list[str]:
    refs = {claim.claim_ref for claim in briefing.claims}
    refs.update(change.claim_ref for change in briefing.changes)
    refs.update(score.claim_ref for score in briefing.scores)
    return sorted(refs)


def _body_lines(briefing: BriefingResult) -> list[str]:
    body = briefing.envelope.body
    if isinstance(body, MorningBody):
        lines = [*body.overnight_changes, body.today_baseline]
        lines.extend(body.style_sector_hypotheses)
        return [line for line in lines if line]
    if isinstance(body, MiddayBody):
        lines = [
            *body.confirmed_hypothesis_refs,
            *body.falsified_hypothesis_refs,
            *body.new_drivers,
            *body.afternoon_risks,
        ]
        return [line for line in lines if line]
    if isinstance(body, EveningBody):
        lines = [*body.prediction_scorecard, *body.errors_and_unresolved]
        return [line for line in lines if line]
    return []


def _first_sentence(text: str) -> str:
    for separator in ("。", "；", "\n"):
        head, found, _rest = text.partition(separator)
        if found and head:
            return f"{head}{separator}"
    return text


def _body_text(
    briefing: BriefingResult, channel: Channel, lines: list[str]
) -> str:
    summary = briefing.envelope.summary
    if channel == "audio":
        return _first_sentence(summary)
    if channel == "text":
        return "\n".join([briefing.envelope.headline, summary])
    if channel == "app":
        return "\n".join([summary, *lines[:2]])
    return "\n".join([summary, "", *lines])


def render(briefing: BriefingResult, channel: str) -> RenderedBriefing:
    """把简报投影到指定通道；未知通道显式拒绝。"""
    if channel not in _CHANNELS:
        raise ValueError(f"unknown briefing render channel: {channel!r}")
    resolved = cast(Channel, channel)
    envelope = briefing.envelope
    return RenderedBriefing(
        briefing_id=envelope.briefing_id,
        channel=resolved,
        headline=envelope.headline,
        direction=briefing.direction,
        relation=envelope.relation,
        risk_level=briefing.risk_level,
        confidence=briefing.confidence,
        evidence_ids=list(envelope.evidence_ids),
        claim_refs=_claim_refs(briefing),
        body_text=_body_text(briefing, resolved, _body_lines(briefing)),
    )


__all__ = ["Channel", "RenderedBriefing", "render"]
