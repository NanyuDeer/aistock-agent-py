"""跨通道简报投影测试（V4-09-P01 / Task 9 Step 4）。

约束：
- App / Web / text / audio 四种投影的 headline、direction、relation、risk level、
  evidence IDs 与 confidence 必须完全一致；
- audio 可以缩短措辞，但不得新增 claim，也不得改变 confidence；
- renderer 只做投影，不含研究逻辑、不调用工具、不读系统时间。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aistock_agent.brain_v4.features.briefing.adapter import load_briefing_chain
from aistock_agent.brain_v4.features.briefing.evaluation import market_trace_reference
from aistock_agent.brain_v4.features.briefing.projection import (
    BriefingChainResult,
    project_chain,
)
from aistock_agent.brain_v4.features.market_trace.adapter import load_frozen_snapshot
from aistock_agent.brain_v4.features.market_trace.profile import run_market_trace
from aistock_agent.brain_v4.renderers.briefing import render

_REPO_ROOT = Path(__file__).resolve().parents[3]
_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "fixtures"
    / "brain_v4"
    / "briefing"
    / "day_chain.json"
)
_RENDERER_SOURCE = (
    _REPO_ROOT / "src" / "aistock_agent" / "brain_v4" / "renderers" / "briefing.py"
)
_CHANNELS = ("app", "web", "text", "audio")
_BANNED_SOURCE_TOKENS = (
    "ToolGateway",
    "run_analyst",
    "create_react_agent",
    "httpx",
    "requests.",
    "datetime.now(",
    "date.today(",
)


def _result() -> BriefingChainResult:
    chain = load_briefing_chain(_FIXTURE)
    case = load_frozen_snapshot(_REPO_ROOT / chain.market_trace.case_fixture)
    run = run_market_trace(case)
    return project_chain(chain, market_trace_reference(run))


def test_render_preserves_shared_contract_fields_across_channels() -> None:
    result = _result()
    rendered = {channel: render(result.evening, channel) for channel in _CHANNELS}

    base = rendered["web"]
    for channel, projection in rendered.items():
        assert projection.channel == channel
        assert projection.headline == base.headline
        assert projection.direction == base.direction
        assert projection.relation == base.relation
        assert projection.risk_level == base.risk_level
        assert projection.evidence_ids == base.evidence_ids
        assert projection.confidence == base.confidence
        assert projection.claim_refs == base.claim_refs


def test_audio_shortens_wording_without_adding_claims_or_confidence_drift() -> None:
    result = _result()
    web = render(result.evening, "web")
    audio = render(result.evening, "audio")

    assert len(audio.body_text) <= len(web.body_text)
    assert audio.confidence == web.confidence
    assert audio.claim_refs == web.claim_refs

    allowed = {score.claim_ref for score in result.evening.scores}
    # 晚报新增的“盘后复核”claim 属于自身 claims（非评分项），也允许出现在投影里。
    allowed |= {claim.claim_ref for claim in result.evening.claims}
    assert set(audio.claim_refs) <= allowed
    spoken_refs = set(re.findall(r"hyp_[a-z_]+", audio.body_text))
    assert spoken_refs <= allowed


def test_render_is_deterministic() -> None:
    result = _result()
    first = render(result.morning, "text")
    second = render(result.morning, "text")
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_unknown_channel_is_rejected() -> None:
    result = _result()
    with pytest.raises(ValueError):
        render(result.morning, "push")


def test_renderer_never_calls_tools_or_reads_system_clock() -> None:
    text = _RENDERER_SOURCE.read_text(encoding="utf-8")
    offenders = [token for token in _BANNED_SOURCE_TOKENS if token in text]
    assert offenders == []
