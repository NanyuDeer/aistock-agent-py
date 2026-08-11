"""ground_truth —— 标准答案自动采集与置信度判定"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.ground_truth import generate_ground_truth, list_pending_review


@pytest.mark.asyncio
async def test_generate_ground_truth_high_confidence(iterate_data_dir: object) -> None:
    case = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "iterate" / "sample_case_review.json")
        .read_text(encoding="utf-8")
    )
    llm_payload = {
        "confidence": "high",
        "attribution": {
            "direction": "bullish",
            "drivers": ["隔夜美股暴涨"],
            "transmission_path": ["美股 → A股高开"],
            "affected_sectors": ["半导体"],
            "source_notes": [{"source": "财联社", "title": "x", "url": "http://x"}],
        },
    }
    with patch(
        "aistock_agent.services.tavily.TavilyService.search",
        return_value={
            "results": [
                {"title": "券商解读", "url": "http://x", "content": "隔夜美股带动 A 股高开"}
            ]
        },
    ), patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=type("R", (), {"content": json.dumps(llm_payload)})()
        )
        gt = await generate_ground_truth(case, max_results=3)

    assert gt["case_id"] == case["case_id"]
    assert gt["confidence"] == "high"
    assert set(gt["attribution"]) == {
        "direction",
        "drivers",
        "transmission_path",
        "affected_sectors",
        "source_notes",
    }
    assert (
        Path(iterate_data_dir) / "ground_truths" / f"{gt['gt_id']}.json"
    ).exists()  # type: ignore[arg-type]


def test_pending_review_lists_low_confidence(iterate_data_dir: object) -> None:
    pending = list_pending_review()
    assert any(item["gt_id"] == "gt_pending_low_confidence" for item in pending)
