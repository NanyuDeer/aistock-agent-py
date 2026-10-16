"""alert 解说 prompt 模板单测。"""
import pytest

from aistock_agent.prompts.workers.alert_reasoning import (
    ALERT_REASONING_FALLBACKS,
    render_alert_reasoning_prompt,
)


def test_scan_prompt_contains_symbol_and_directions():
    p = render_alert_reasoning_prompt(
        scene="alert_scan", symbol="600519", cycle="短线（1-5天）",
        directions="资讯情报、盘口风控、图谱发散",
    )
    assert "600519" in p
    assert "资讯情报、盘口风控、图谱发散" in p
    assert "第一人称" in p


def test_master_prompt_contains_digest():
    p = render_alert_reasoning_prompt(
        scene="alert_master", symbol="600519", cycle="短线（1-5天）",
        digest="【资讯情报】有公告；【盘口风控】放量；【图谱发散】同板块联动",
    )
    assert "600519" in p
    assert "有公告" in p


def test_heartbeat_prompt_contains_stage_and_elapsed():
    p = render_alert_reasoning_prompt(
        scene="alert_heartbeat", symbol="600519", stage="汇聚研判",
        elapsed_sec=16, done_steps="多维分析",
    )
    assert "汇聚研判" in p
    assert "16" in p
    assert "多维分析" in p


def test_all_scenes_forbid_fabrication_and_json():
    for scene in ("alert_scan", "alert_master", "alert_heartbeat"):
        p = render_alert_reasoning_prompt(scene=scene, symbol="600519")
        assert "禁止输出 JSON" in p
        assert "禁止" in p and "编造" in p


def test_unknown_scene_raises_keyerror():
    with pytest.raises(KeyError):
        render_alert_reasoning_prompt(scene="nope", symbol="600519")


def test_fallbacks_cover_all_nodes():
    assert set(ALERT_REASONING_FALLBACKS) == {"alert_scan", "alert_master"}
