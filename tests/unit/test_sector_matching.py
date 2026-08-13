"""板块别名字典 + 两级匹配测试"""
import json
from pathlib import Path


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


def test_sector_code_match_aliases_for_granularity_gap():
    """别名覆盖粗/细粒度缺口：morning 粗方向命中 review 细概念（2026-08-05 实况）。

    晨报预测"AI/CPO/半导体"，复盘实际领涨"存储芯片/光刻机/中芯国际概念"，
    语义同属半导体产业链，应判定命中而非全部落空。
    """
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning_sectors = ["AI/CPO/半导体", "新型电力系统/特高压", "CRO/医药", "石油石化", "黄金/有色"]
    review_sectors = [
        "MLCC概念", "中芯国际概念", "国家大基金持股", "存储芯片",
        "光刻机", "芬太尼", "医药电商", "禽流感", "白酒概念", "可燃冰",
    ]

    overlap, missing, over_focused = match_sectors_code_level(
        morning_sectors, review_sectors
    )
    # AI/CPO/半导体 命中 存储芯片/光刻机/中芯国际概念/国家大基金持股
    assert "AI/CPO/半导体" in overlap
    # CRO/医药 命中 医药电商（同属医药）
    assert "CRO/医药" in overlap
    # 真实未预测到的方向仍计入 missing
    assert "芬太尼" in missing
    assert "禽流感" in missing
    assert "可燃冰" in missing
    # 真实未兑现的晨报方向计入 over_focused
    assert "新型电力系统/特高压" in over_focused


def test_sector_code_match_contains_fallback():
    """包含匹配兜底：未登记别名但语义包含的板块也能命中。"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning_sectors = ["半导体"]
    review_sectors = ["半导体设备", "银行"]

    overlap, missing, _ = match_sectors_code_level(morning_sectors, review_sectors)
    assert "半导体" in overlap  # 半导体 ⊂ 半导体设备 → 命中
    assert "银行" in missing  # 银行与半导体无包含关系 → 不误判


def test_sector_code_match_no_false_positive_on_unrelated():
    """包含匹配不得把不相关板块误判为命中。"""
    from aistock_agent.services.snapshot_builder import match_sectors_code_level

    morning_sectors = ["银行", "白酒"]
    review_sectors = ["半导体", "煤炭"]

    overlap, _, over_focused = match_sectors_code_level(morning_sectors, review_sectors)
    assert overlap == []
    assert set(over_focused) == {"银行", "白酒"}
