from unittest.mock import patch

import pytest

from aistock_agent.schemas.chat_contract import InsightGoal
from aistock_agent.skills.douyin_video import douyin_video


def _goal() -> InsightGoal:
    return InsightGoal(
        question="分析这个抖音视频 https://v.douyin.com/abc/",
        intent="douyin_video",
        time_range="today",
    )


@pytest.mark.asyncio
async def test_douyin_video_returns_evidence_with_transcript():
    client_result = {
        "video_info": {"video_id": "123", "title": "测试", "url": "https://x"},
        "text": "转写全文内容",
        "output_path": "c:/tmp/123",
    }
    with patch(
        "aistock_agent.skills.douyin_video.DouyinClient",
        autospec=True,
    ) as mock_cls:
        mock_client = mock_cls.return_value
        mock_client.extract_text.return_value = client_result

        ev = await douyin_video(
            {"link": "https://v.douyin.com/abc/", "save_video": False}, _goal()
        )

    assert ev.degraded is False
    assert ev.skill_name == "douyin_video"
    assert any("转写全文内容" in f for f in ev.facts)
    assert ev.raw["transcript_path"] == "c:/tmp/123"


@pytest.mark.asyncio
async def test_douyin_video_degrades_on_missing_link():
    ev = await douyin_video({}, _goal())
    assert ev.degraded is True
    assert "link" in (ev.degraded_reason or "")
