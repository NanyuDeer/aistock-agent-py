"""alert 流式帧时序单测 — preview 先于 result、reasoning 帧存在。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import alert as alert_mod

_ALERT_MOD = "aistock_agent.agents.workers.alert"


async def _collect(state, *, preview=None, detail=None, delay=0.0):
    """跑 stream() 收集所有帧。preview/detail 为 (fields, podcast) 或 dict。"""
    async def fake_preview(symbol, cycle, reports):
        await asyncio.sleep(delay)
        return preview if preview is not None else {
            "summary": "异动结论", "impact": "利好", "keywords": ["涨价"],
        }

    async def fake_detail(symbol, cycle, reports):
        await asyncio.sleep(delay)
        return (
            {"details": "## 详情", "stocks": ["600519"], "risks": ["风险A"]},
            "异动摘要",
        )

    with (
        patch(f"{_ALERT_MOD}._run_sub_agent", new=AsyncMock(return_value="子报告")),
        patch(f"{_ALERT_MOD}._run_master_preview", new=fake_preview),
        patch(f"{_ALERT_MOD}._run_master_detail", new=fake_detail),
        patch(f"{_ALERT_MOD}.stream_reasoning_text", new=AsyncMock()) as mock_reason,
        patch(f"{_ALERT_MOD}.render_alert_reasoning_prompt", return_value="PROMPT"),
        patch(f"{_ALERT_MOD}.node_api.save_analysis_report", new=AsyncMock()),
        patch(f"{_ALERT_MOD}._cache_alert_result"),
    ):
        frames = [f async for f in alert_mod.stream(state)]
    return frames, mock_reason


@pytest.mark.asyncio
async def test_preview_frame_precedes_result():
    frames, _ = await _collect({"symbol": "600519"})
    types = [f.get("type") for f in frames]
    assert "preview" in types and "result" in types
    assert types.index("preview") < types.index("result")
    preview = frames[types.index("preview")]
    assert preview["display_report"]["summary"] == "异动结论"


@pytest.mark.asyncio
async def test_result_contains_merged_six_fields():
    frames, _ = await _collect({"symbol": "600519"})
    result = next(f for f in frames if f.get("type") == "result")
    assert result["display_report"] == {
        "summary": "异动结论",
        "impact": "利好",
        "keywords": ["涨价"],
        "details": "## 详情",
        "stocks": ["600519"],
        "risks": ["风险A"],
    }
    assert result["podcast_brief"] == "异动摘要"


@pytest.mark.asyncio
async def test_reasoning_started_once_per_phase():
    """两个阶段各启动一次解说（心跳由独立循环承担，不在本断言内）。"""
    _, mock_reason = await _collect({"symbol": "600519"})
    nodes = [c.kwargs["node"] for c in mock_reason.call_args_list]
    assert nodes.count("alert_scan") == 1
    assert nodes.count("alert_master") == 1


@pytest.mark.asyncio
async def test_stream_ends_with_done():
    frames, _ = await _collect({"symbol": "600519"})
    assert frames[-1].get("type") == "done"
