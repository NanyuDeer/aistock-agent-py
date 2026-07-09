"""快照生成器 core 测试 — 文件I/O、MA计算、manifest、板块匹配"""
import json
from pathlib import Path
from unittest.mock import patch

import pytest


def test_match_sectors_code_level_basic():
    """第一级板块匹配：精确 + 别名"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning = ["黄金", "军工", "新能源车"]
    review = ["贵金属", "国防军工", "半导体"]

    overlap, missing, over_focused = match_sectors_code_level(morning, review)
    assert "黄金" in overlap  # 别名匹配 贵金属
    assert "军工" in overlap  # 别名匹配 国防军工
    assert "半导体" in missing
    assert "新能源车" in over_focused


def test_calculate_ma5_empty_manifest():
    """空 manifest 时 MA5 返回零值"""
    from aistock_agent.services.snapshot_builder import calculate_ma

    stats = calculate_ma([], window=5)
    assert stats["hit_rate"] == 0.0
    assert stats["direction_accuracy"] == 0.0


def test_calculate_ma5_with_records():
    """5条记录计算 MA5"""
    from aistock_agent.services.snapshot_builder import calculate_ma

    records = [
        {"hit_rate": 0.6, "direction_accuracy": 0.5, "mean_deviation": 1.0,
         "attribution_match_rate": 0.4, "sentiment_bias": 0.1},
        {"hit_rate": 0.7, "direction_accuracy": 0.6, "mean_deviation": 1.2,
         "attribution_match_rate": 0.5, "sentiment_bias": 0.2},
        {"hit_rate": 0.5, "direction_accuracy": 0.4, "mean_deviation": 0.8,
         "attribution_match_rate": 0.3, "sentiment_bias": 0.05},
        {"hit_rate": 0.8, "direction_accuracy": 0.7, "mean_deviation": 1.5,
         "attribution_match_rate": 0.6, "sentiment_bias": 0.15},
        {"hit_rate": 0.65, "direction_accuracy": 0.55, "mean_deviation": 1.1,
         "attribution_match_rate": 0.45, "sentiment_bias": 0.12},
    ]
    stats = calculate_ma(records, window=5)
    assert 0.6 < stats["hit_rate"] < 0.7  # 平均值在合理范围
    assert stats["direction_accuracy"] > 0


def test_update_manifest_append():
    """manifest 追加新记录"""
    from aistock_agent.services.snapshot_builder import update_manifest

    existing = {"records": [{"date": "2026-07-07", "snapshot_file": "...", "hit_rate": 0.6}]}
    new_record = {"date": "2026-07-08", "snapshot_file": "...", "hit_rate": 0.7}
    updated = update_manifest(existing, new_record)
    assert len(updated["records"]) == 2
    assert updated["records"][-1]["date"] == "2026-07-08"


def test_build_snapshot_degraded_when_files_missing(tmp_path):
    """晨报/复盘文件不存在时，生成降级快照（标注 error）"""
    from aistock_agent.services.snapshot_builder import build_snapshot

    result = build_snapshot(date_str="2026-07-08")
    assert result["date"] == "2026-07-08"
    assert result.get("error") is not None or result.get("dimension_1_coverage", {}).get("hit_rate") == 0.0
