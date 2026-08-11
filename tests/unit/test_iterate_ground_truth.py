"""ground_truth —— 标准答案自动采集与置信度判定"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.iterate.ground_truth import (
    _direction_from_snapshot,
    _top_gainers,
    generate_data_constrained_gt,
    generate_ground_truth,
    list_pending_review,
)


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
    assert set(cast("dict[str, object]", gt["attribution"])) == {
        "direction",
        "drivers",
        "transmission_path",
        "affected_sectors",
        "source_notes",
    }
    assert (
        Path(str(iterate_data_dir)) / "ground_truths" / f"{gt['gt_id']}.json"
    ).exists()


def test_pending_review_lists_low_confidence(iterate_data_dir: object) -> None:
    pending = list_pending_review()
    assert any(item["gt_id"] == "gt_pending_low_confidence" for item in pending)


def _a_share() -> dict[str, object]:
    return {
        "indexes": {"SH000001": {"name": "上证指数", "change_pct": 1.2}},
        "sectors": {
            "top_gainers": [{"name": "半导体"}, {"name": "算力"}, {"name": "新能源"}],
            "top_losers": [{"name": "白酒"}],
            "top_inflows": [],
            "top_outflows": [],
        },
    }


def _case() -> dict[str, object]:
    return {
        "case_id": "case_t",
        "ground_truth_ref": "gt_case_t",
        "event_title": "隔夜美股暴涨，A股高开",
        "event_time": "2026-07-31T09:30:00+08:00",
        "window_before": {
            "cls_telegraph": [
                {
                    "time": "2026-07-31T09:00:00+08:00",
                    "title": "隔夜美股暴涨",
                    "content": "纳斯达克涨2.5%",
                    "url": "u1",
                }
            ],
            "market_snapshot": {"a_share": _a_share(), "sources": {}},
            "global_markets": [
                {
                    "ticker": "^IXIC",
                    "change_pct": 2.5,
                    "asof": "2026-07-31T04:00:00+08:00",
                }
            ],
        },
    }


def test_direction_from_snapshot_thresholds() -> None:
    assert _direction_from_snapshot({"indexes": {"S": {"change_pct": 1.2}}}) == "bullish"
    assert _direction_from_snapshot({"indexes": {"S": {"change_pct": -1.2}}}) == "bearish"
    assert _direction_from_snapshot({"indexes": {"S": {"change_pct": 0.3}}}) == "neutral"
    assert _direction_from_snapshot({"indexes": {}}) == "neutral"


def test_top_gainers_extracts_names() -> None:
    assert _top_gainers(_a_share(), n=3) == ["半导体", "算力", "新能源"]
    assert _top_gainers({"sectors": {}}, n=3) == []


@pytest.mark.asyncio
async def test_generate_data_constrained_gt_deterministic_fields(
    tmp_path: Path,
) -> None:
    """方向/板块确定性；drivers 由 LLM 受约束提取（mock）。"""
    llm_payload = {"drivers": ["隔夜美股暴涨", "外盘传导"]}
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content=json.dumps(llm_payload, ensure_ascii=False))
        )
        gt = await generate_data_constrained_gt(_case(), data_dir=tmp_path)

    attribution = cast("dict[str, object]", gt["attribution"])
    assert attribution["direction"] == "bullish"
    assert attribution["affected_sectors"] == ["半导体", "算力", "新能源"]
    assert attribution["drivers"] == ["隔夜美股暴涨", "外盘传导"]
    assert gt["gt_id"] == "gt_case_t"

    # 驱动提取 prompt 必须只含切片语料（断言含电报标题，且含禁止后验要求）
    prompt_arg = factory.return_value.ainvoke.call_args.args[0][0].content
    assert "隔夜美股暴涨" in prompt_arg
    assert "禁止" in prompt_arg and "语料之外" in prompt_arg


@pytest.mark.asyncio
async def test_generate_data_constrained_gt_llm_fallback(tmp_path: Path) -> None:
    """drivers LLM 失败时降级为确定性摘要（不崩）。"""
    with patch("aistock_agent.services.llm.get_deep_think") as factory:
        factory.return_value.ainvoke = AsyncMock(
            return_value=SimpleNamespace(content="not json")
        )
        gt = await generate_data_constrained_gt(_case(), data_dir=tmp_path)
    drivers = cast("dict[str, object]", gt["attribution"])["drivers"]
    assert isinstance(drivers, list)
    assert drivers  # 非空
