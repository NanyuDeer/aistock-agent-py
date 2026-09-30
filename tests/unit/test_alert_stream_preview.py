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


@pytest.mark.asyncio
async def test_detail_exception_converges_and_reclaims_tasks():
    """审阅问题 1：_run_master_detail 抛异常时不得泄漏孤儿任务、须在超时内收敛出 error 帧。

    修复前：异常落到 except 后再无 finally，phase2_stop 不置位、仍在跑的
    detail 深度调用成为孤儿任务 → 用例因残留任务失败；修复后 finally 全量回收。
    """
    async def fake_preview(symbol, cycle, reports):
        return {"summary": "异动结论", "impact": "利好", "keywords": ["涨价"]}

    async def boom_detail(symbol, cycle, reports):
        await asyncio.sleep(0.2)          # 保证在回收前仍是"在运行"的任务
        raise RuntimeError("detail boom")  # 异动详情失败

    with (
        patch(f"{_ALERT_MOD}._run_sub_agent", new=AsyncMock(return_value="子报告")),
        patch(f"{_ALERT_MOD}._run_master_preview", new=fake_preview),
        patch(f"{_ALERT_MOD}._run_master_detail", new=boom_detail),
        patch(f"{_ALERT_MOD}.stream_reasoning_text", new=AsyncMock()),
        patch(f"{_ALERT_MOD}.render_alert_reasoning_prompt", return_value="PROMPT"),
        patch(f"{_ALERT_MOD}.node_api.save_analysis_report", new=AsyncMock()),
        patch(f"{_ALERT_MOD}._cache_alert_result"),
        patch(f"{_ALERT_MOD}.ALERT_REASONING_HEARTBEAT_SEC", 0.05),
    ):
        before = set(asyncio.all_tasks())

        async def _run():
            return [f async for f in alert_mod.stream({"symbol": "600519"})]

        frames = await asyncio.wait_for(_run(), timeout=3.0)
        leftover = set(asyncio.all_tasks()) - before

    assert any(f.get("type") == "error" for f in frames)
    assert not leftover  # 修复前 detail 孤儿任务会残留，此处失败


@pytest.mark.asyncio
async def test_aclose_stops_bg_tasks_without_leak():
    """审阅问题 2：消费方 aclose()（GeneratorExit）必须回收全部后台任务、不抛异常。

    修复前无 finally，GeneratorExit 穿过 stream() 后后台任务（含仍在跑的 detail）
    全部孤儿化 → leftover 非空失败；修复后 finally 无条件回收。
    """
    async def fake_preview(symbol, cycle, reports):
        return {"summary": "异动结论", "impact": "利好", "keywords": ["涨价"]}

    async def slow_detail(symbol, cycle, reports):
        await asyncio.sleep(5.0)
        return ({"details": "## 详情"}, "摘要")

    with (
        patch(f"{_ALERT_MOD}._run_sub_agent", new=AsyncMock(return_value="子报告")),
        patch(f"{_ALERT_MOD}._run_master_preview", new=fake_preview),
        patch(f"{_ALERT_MOD}._run_master_detail", new=slow_detail),
        patch(f"{_ALERT_MOD}.stream_reasoning_text", new=AsyncMock()),
        patch(f"{_ALERT_MOD}.render_alert_reasoning_prompt", return_value="PROMPT"),
        patch(f"{_ALERT_MOD}.node_api.save_analysis_report", new=AsyncMock()),
        patch(f"{_ALERT_MOD}._cache_alert_result"),
        patch(f"{_ALERT_MOD}.ALERT_REASONING_HEARTBEAT_SEC", 0.05),
    ):
        before = set(asyncio.all_tasks())
        agen = alert_mod.stream({"symbol": "600519"})
        taken = 0
        async for _f in agen:
            taken += 1
            if taken >= 4:            # 已越过 phase2 任务创建点、detail 仍在运行，此刻断连
                await agen.aclose()
                break
        leftover = set(asyncio.all_tasks()) - before

    assert not leftover  # 修复前 slow_detail 等后台任务孤儿化，此处失败
