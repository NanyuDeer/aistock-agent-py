"""alert 心跳解说的节奏与上限单测。"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.agents.workers import alert as alert_mod

_ALERT_MOD = "aistock_agent.agents.workers.alert"


@pytest.mark.asyncio
async def test_heartbeat_stops_when_stage_ends():
    """阶段结束（stop_event 置位）后心跳立即停止，不再调用 LLM。"""
    sink = AsyncMock()
    stop = asyncio.Event()

    with patch(f"{_ALERT_MOD}.stream_reasoning_text", new=AsyncMock()) as mock_reason, \
         patch(f"{_ALERT_MOD}.render_alert_reasoning_prompt", return_value="P"), \
         patch(f"{_ALERT_MOD}.ALERT_REASONING_HEARTBEAT_SEC", 0.05), \
         patch(f"{_ALERT_MOD}.ALERT_REASONING_MAX_HEARTBEATS", 8):
        task = asyncio.create_task(alert_mod._heartbeat_loop(
            sink, node="alert_scan", symbol="600519", stage="多维分析",
            stop_event=stop, done_steps=["多维分析"],
        ))
        await asyncio.sleep(0.12)   # 至少放行 2 次心跳
        stop.set()
        await asyncio.wait_for(task, timeout=1.0)

    beats = mock_reason.await_count
    assert 1 <= beats <= 8
    await asyncio.sleep(0.2)
    assert mock_reason.await_count == beats   # 停止后不再增加


@pytest.mark.asyncio
async def test_heartbeat_respects_max_beats():
    """上限 max_beats 生效（阶段不结束时自然收敛）。"""
    sink = AsyncMock()
    stop = asyncio.Event()

    with patch(f"{_ALERT_MOD}.stream_reasoning_text", new=AsyncMock()) as mock_reason, \
         patch(f"{_ALERT_MOD}.render_alert_reasoning_prompt", return_value="P"), \
         patch(f"{_ALERT_MOD}.ALERT_REASONING_HEARTBEAT_SEC", 0.01), \
         patch(f"{_ALERT_MOD}.ALERT_REASONING_MAX_HEARTBEATS", 3):
        await asyncio.wait_for(alert_mod._heartbeat_loop(
            sink, node="alert_scan", symbol="600519", stage="多维分析",
            stop_event=stop, done_steps=["多维分析"],
        ), timeout=2.0)

    assert mock_reason.await_count == 3
