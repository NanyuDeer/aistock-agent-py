"""午报播报 Agent 单元测试（方案 A：读 midday → 双人对话 → 音频回填 audio_path）。"""
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.agents.workers import midday_broadcast as mb

# ── _extract_midday_brief（素材提取）──

def _content(*, podcast_brief: str, details: str = "D") -> dict:
    return {
        "display_report": {"details": details, "stocks": [], "risks": []},
        "podcast_brief": podcast_brief,
        "schema_version": "2.0",
    }


def _report(content: dict) -> dict:
    return {"id": 1, "report_type": "midday", "content": content}


def test_extract_midday_brief_prefers_podcast_brief():
    report = _report(_content(podcast_brief="摘要", details="很长的详情"))
    assert mb._extract_midday_brief(report) == "摘要"


def test_extract_midday_brief_falls_back_to_details_capped():
    report = _report(_content(podcast_brief="", details="X" * 600))
    assert len(mb._extract_midday_brief(report)) == mb._MIDDAY_BRIEF_CAP


def test_extract_midday_brief_empty_on_bad_input():
    assert mb._extract_midday_brief(None) == ""
    assert mb._extract_midday_brief({"no": "content"}) == ""


# ── run() ──

@pytest.mark.asyncio
async def test_run_skips_non_trading_day():
    with patch.object(mb, "is_trading_day", return_value=False):
        result = await mb.run({"report_date": "2026-08-23"})
    assert result["midday_broadcast"]["generated"] is False
    assert result["midday_broadcast"]["audio_path"] is None


@pytest.mark.asyncio
async def test_run_material_missing_no_audio_call():
    with (
        patch.object(mb, "is_trading_day", return_value=True),
        patch.object(mb.node_api, "get_analysis_report", AsyncMock(return_value=None)),
        patch.object(mb.node_api, "post") as post_mock,
    ):
        result = await mb.run({"report_date": "2026-08-24"})
    assert result["midday_broadcast"]["generated"] is False
    post_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_success_posts_audio_and_returns_path():
    report = _report(_content(podcast_brief="摘要"))
    dialogue_json = (
        '[{"role":"host","content":"午间收盘了。"},'
        '{"role":"analyst","content":"沪指收涨。"}]'
    )
    fake_model = Mock()
    fake_model.ainvoke = AsyncMock(return_value=AIMessage(content=dialogue_json))
    post_mock = AsyncMock(return_value={"audio_path": "/api/agent/audio/midday-2026-08-24.mp3"})

    with (
        patch.object(mb, "is_trading_day", return_value=True),
        patch.object(mb.node_api, "get_analysis_report", AsyncMock(return_value=report)),
        patch.object(mb, "get_deep_think", return_value=fake_model),
        patch.object(mb.node_api, "post", post_mock),
    ):
        result = await mb.run({"report_date": "2026-08-24"})

    assert result["midday_broadcast"]["generated"] is True
    assert result["midday_broadcast"]["audio_path"] == "/api/agent/audio/midday-2026-08-24.mp3"
    expected_dialogue = [
        {"role": "host", "content": "午间收盘了。"},
        {"role": "analyst", "content": "沪指收涨。"},
    ]
    post_mock.assert_awaited_once_with(
        "/internal/midday/generate-audio",
        {"date": "2026-08-24", "dialogue": expected_dialogue},
        timeout=300.0,
    )


@pytest.mark.asyncio
async def test_run_audio_failure_no_audio_path():
    report = _report(_content(podcast_brief="摘要"))
    dialogue_json = '[{"role":"host","content":"午间收盘了。"}]'
    fake_model = Mock()
    fake_model.ainvoke = AsyncMock(return_value=AIMessage(content=dialogue_json))

    with (
        patch.object(mb, "is_trading_day", return_value=True),
        patch.object(mb.node_api, "get_analysis_report", AsyncMock(return_value=report)),
        patch.object(mb, "get_deep_think", return_value=fake_model),
        patch.object(mb.node_api, "post", AsyncMock(return_value=None)),
    ):
        result = await mb.run({"report_date": "2026-08-24"})
    assert result["midday_broadcast"]["generated"] is False
    assert result["midday_broadcast"]["audio_path"] is None


@pytest.mark.asyncio
async def test_run_exception_degraded_no_raise():
    with (
        patch.object(mb, "is_trading_day", return_value=True),
        patch.object(
            mb.node_api,
            "get_analysis_report",
            AsyncMock(side_effect=RuntimeError("boom")),
        ),
    ):
        result = await mb.run({"report_date": "2026-08-24"})
    assert result["midday_broadcast"]["generated"] is False
    assert result["midday_broadcast"]["audio_path"] is None
    assert "暂" in result["final_response"] or "不可用" in result["final_response"]
