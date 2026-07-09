"""板块别名字典 + 两级匹配测试"""
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest


def _load_aliases() -> dict[str, list[str]]:
    """加载板块别名字典"""
    aliases_path = Path("src/aistock_agent/data/sector_aliases.json")
    return json.loads(aliases_path.read_text(encoding="utf-8"))


def test_sector_aliases_loads_valid_json():
    """字典文件是合法 JSON，且至少有 30 条映射"""
    aliases = _load_aliases()
    assert isinstance(aliases, dict)
    assert len(aliases) >= 30


def test_sector_aliases_values_are_lists():
    """每个 key 的 value 是字符串列表"""
    aliases = _load_aliases()
    for key, val in aliases.items():
        assert isinstance(key, str)
        assert isinstance(val, list)
        assert all(isinstance(v, str) for v in val)


def test_sector_code_match_exact():
    """第一级：代码字典精确匹配 — 板块名完全一致"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning_sectors = ["黄金", "军工", "新能源车"]
    review_sectors = ["黄金", "军工", "半导体"]

    overlap, missing, over_focused = match_sectors_code_level(
        morning_sectors, review_sectors
    )
    assert set(overlap) == {"黄金", "军工"}
    assert set(missing) == {"半导体"}  # 复盘有、晨报没有
    assert set(over_focused) == {"新能源车"}  # 晨报有、复盘没有


def test_sector_code_match_alias():
    """第一级：代码字典别名匹配 — 晨报"黄金" 匹配 复盘"贵金属" """
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning_sectors = ["黄金", "军工"]
    review_sectors = ["贵金属", "国防军工", "半导体"]

    overlap, missing, over_focused = match_sectors_code_level(
        morning_sectors, review_sectors
    )
    assert "黄金" in overlap  # 黄金→贵金属 命中
    assert "军工" in overlap  # 军工→国防军工 命中
    assert "半导体" in missing
