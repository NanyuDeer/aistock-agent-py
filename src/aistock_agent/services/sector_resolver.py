"""板块中文名 → BK 代码解析（M2 sector 板块代码对齐）。

背景：Node `GET /internal/leader/:tagCode` 硬校验 `^BK\\d{4}$`（非 BK 格式 400），
而 qa_router 的 goal.tag_codes 是中文板块名。本模块在 Python 本地完成
"中文板块名 → BK 代码" 映射，未命中返回 None（由调用方回落无 tag_code 模式）。

设计约束（D22/D23 偏差）：
- sector_aliases.json 结构为 `{标准板块名: [别名...]}`，人工维护；运行时学到的别名
  写在独立的 sector_aliases_learned.json（gitignore），读取时由
  sector_aliases_store.load_merged_aliases 合并（仓库优先）。
  因此映射表独立为新增的 sector_tag_codes.json（`{标准名: "BK0477"}`）。
- 查找顺序：sector_tag_codes.json 标准名精确命中 → 别名字典反向匹配
  （别名 → 标准名 → tag_code）→ 未命中 None。
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from aistock_agent.services.sector_aliases_store import load_merged_aliases

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_TAG_CODES_FILE = _DATA_DIR / "sector_tag_codes.json"


@lru_cache(maxsize=1)
def _load_tag_codes_cached() -> tuple[bool, dict[str, str]]:
    """缓存只存「加载成功」；失败返回 (False, {})，由调用方负责 warning。"""
    try:
        with _TAG_CODES_FILE.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError) as exc:
        logger.warning("板块映射表缺失（sector_tag_codes.json）：%s", exc)
        return False, {}
    if not isinstance(raw, dict):
        return False, {}
    codes = {
        name: code
        for name, code in raw.items()
        if isinstance(name, str) and isinstance(code, str) and code
    }
    return bool(codes), codes


def _load_tag_codes() -> dict[str, str]:
    ok, codes = _load_tag_codes_cached()
    if not ok:
        return {}
    return codes


@lru_cache(maxsize=1)
def _load_alias_index() -> dict[str, str]:
    """别名 → 标准板块名 反向索引（只读）。

    数据源为 sector_aliases_store.load_merged_aliases（仓库字典 + 运行时 learned，
    仓库优先；learned 缺失/损坏时退化为仓库内容，不抛错）。
    """
    index: dict[str, str] = {}
    for standard_name, aliases in load_merged_aliases().items():
        index[standard_name] = standard_name
        for alias in aliases:
            if alias:
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
