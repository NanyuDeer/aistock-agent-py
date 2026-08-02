"""板块中文名 → BK 代码解析（M2 sector 板块代码对齐）。

背景：Node `GET /internal/leader/:tagCode` 硬校验 `^BK\\d{4}$`（非 BK 格式 400），
而 qa_router 的 goal.tag_codes 是中文板块名。本模块在 Python 本地完成
"中文板块名 → BK 代码" 映射，未命中返回 None（由调用方回落无 tag_code 模式）。

设计约束（D22/D23 偏差）：
- sector_aliases.json 结构为 `{标准板块名: [别名...]}`，被 snapshot_builder 读取写回，
  禁止修改其结构；因此映射表独立为新增的 sector_tag_codes.json（`{标准名: "BK0477"}`）。
- 查找顺序：sector_tag_codes.json 标准名精确命中 → sector_aliases.json 别名反向匹配
  （别名 → 标准名 → tag_code）→ 未命中 None。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_TAG_CODES_FILE = _DATA_DIR / "sector_tag_codes.json"
_ALIASES_FILE = _DATA_DIR / "sector_aliases.json"


@lru_cache(maxsize=1)
def _load_tag_codes() -> dict[str, str]:
    """加载 {标准板块名: BK 代码} 映射；非法值过滤，不抛异常。"""
    try:
        with _TAG_CODES_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        name: code
        for name, code in raw.items()
        if isinstance(name, str) and isinstance(code, str) and code
    }


@lru_cache(maxsize=1)
def _load_alias_index() -> dict[str, str]:
    """sector_aliases.json 别名 → 标准板块名 反向索引（只读，不修改原文件）。"""
    try:
        with _ALIASES_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    index: dict[str, str] = {}
    for standard_name, aliases in raw.items():
        if not isinstance(standard_name, str) or not isinstance(aliases, list):
            continue
        index[standard_name] = standard_name
        for alias in aliases:
            if isinstance(alias, str) and alias:
                index[alias] = standard_name
    return index


def resolve_tag_code(name: str | None) -> str | None:
    """中文板块名 → BK 代码；未命中返回 None（不抛异常）。

    查找顺序：
    1. sector_tag_codes.json 标准名精确命中
    2. sector_aliases.json 别名反向匹配（别名 → 标准名 → tag_code）
    3. 未命中 → None
    """
    if not name or not name.strip():
        return None
    trimmed = name.strip()

    tag_codes = _load_tag_codes()
    if trimmed in tag_codes:
        return tag_codes[trimmed]

    standard_name = _load_alias_index().get(trimmed)
    if standard_name is not None:
        return tag_codes.get(standard_name)

    return None
