"""resolve_tag_code 单元测试（M2 sector 板块代码对齐）。

映射表 sector_tag_codes.json 独立于 sector_aliases.json（结构冻结，禁止修改）。
"""

from aistock_agent.services.sector_resolver import resolve_tag_code


def test_resolve_tag_code_exact_hit() -> None:
    """标准名精确命中（白酒 → BK0477）。"""
    assert resolve_tag_code("白酒") == "BK0477"


def test_resolve_tag_code_semiconductor_exact_hit() -> None:
    """标准名精确命中（半导体 → BK1036）。"""
    assert resolve_tag_code("半导体") == "BK1036"


def test_resolve_tag_code_alias_fallback() -> None:
    """别名反向匹配：'酿酒行业' 是 sector_aliases.json 中白酒的别名 → BK0477。"""
    assert resolve_tag_code("酿酒行业") == "BK0477"


def test_resolve_tag_code_alias_fallback_chip() -> None:
    """别名反向匹配：'芯片' 是半导体的别名 → BK1036。"""
    assert resolve_tag_code("芯片") == "BK1036"


def test_resolve_tag_code_miss_returns_none() -> None:
    """未知名 → None，不抛异常。"""
    assert resolve_tag_code("不存在的板块xyz") is None


def test_resolve_tag_code_empty_input() -> None:
    """空串 / None → None。"""
    assert resolve_tag_code("") is None
    assert resolve_tag_code(None) is None


def test_resolve_tag_code_whitespace_only() -> None:
    """纯空白 → None。"""
    assert resolve_tag_code("   ") is None


def test_resolve_tag_code_standard_name_not_in_tag_codes_returns_none() -> None:
    """sector_aliases.json 有但 tag_codes 表未覆盖的标准名 → None（不抛异常）。"""
    # 例如 sector_aliases.json 中的 "纺织服装" 等未收录板块
    assert resolve_tag_code("纺织服装") is None
