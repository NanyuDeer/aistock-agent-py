"""板块别名字典读写的单点实现（仓库文件 + 运行时学习文件）。

设计约束（2026-09-18）：
  - ``sector_aliases.json`` 是人工维护的权威字典。历史上
    ``snapshot_builder._append_new_aliases`` 在生产运行中直接回写该文件，
    导致服务器工作区长期存在未提交改动、``git pull`` 必然冲突；现运行时学习
    结果只写独立的 ``sector_aliases_learned.json``（已 gitignore），仓库文件
    此后只由人工/提交维护。
  - 读取侧统一走 :func:`load_merged_aliases`：**仓库文件优先**（人工维护即权威），
    learned 只补充仓库里不存在的标准名与别名，不覆盖、不重排仓库条目；
    同名标准名两边都有时整体取仓库条目。
  - learned 文件缺失/损坏时 fail-safe 退化为仓库内容（只 warning，不抛错）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
# 人工维护的权威字典（随提交进仓库）
REPO_ALIASES_FILE = _DATA_DIR / "sector_aliases.json"
# 运行时学习结果（gitignore，不进仓库）
LEARNED_ALIASES_FILE = _DATA_DIR / "sector_aliases_learned.json"


def _read_alias_file(path: Path) -> dict[str, list[str]] | None:
    """读取别名字典文件；缺失/损坏/结构非法时返回 None（只 warning，不抛错）。"""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("aliases_file_unreadable path=%s error=%s", path, exc)
        return None
    if not isinstance(raw, dict):
        logger.warning(
            "aliases_file_invalid_structure path=%s actual_type=%s",
            path,
            type(raw).__name__,
        )
        return None
    cleaned: dict[str, list[str]] = {}
    for standard_name, aliases in raw.items():
        if not isinstance(standard_name, str) or not isinstance(aliases, list):
            continue
        cleaned[standard_name] = [alias for alias in aliases if isinstance(alias, str)]
    return cleaned


def load_repo_aliases() -> dict[str, list[str]]:
    """加载仓库权威别名字典（读取失败返回空字典）。"""
    return _read_alias_file(REPO_ALIASES_FILE) or {}


def load_learned_aliases() -> dict[str, list[str]]:
    """加载运行时学习别名字典（缺失/损坏返回空字典）。"""
    return _read_alias_file(LEARNED_ALIASES_FILE) or {}


def load_merged_aliases() -> dict[str, list[str]]:
    """仓库字典 ∪ learned 字典（**仓库优先，但取并集**）。

    优先级规则（2026-09-18 修订）：
      - learned 的标准名是仓库的**标准名** → **并集**：保留仓库别名（顺序在前），只追加仓库没有的；
        为什么不是"整体取仓库条目"：LLM 最常学到的恰恰是"给已有标准名补别名"
        （如 军工 ← 军工/航海装备），若整体取仓库条目，这条主线学习会变成 no-op。
      - learned 的标准名是仓库的**别名** → 跳过（该名字已被占用，新增同名键会让反向索引歧义）；
      - 别名串已在仓库或本次合并结果中出现 → 跳过（不重复登记）。
    learned 缺失/损坏 → 静默退化为仓库内容。
    """
    repo = load_repo_aliases()
    merged: dict[str, list[str]] = {
        standard_name: list(aliases) for standard_name, aliases in repo.items()
    }
    learned = load_learned_aliases()
    if not learned:
        return merged

    # 已被占用的别名串（仓库侧；随合并推进同步补入，避免重复登记）
    known_aliases: set[str] = set()
    for aliases in repo.values():
        known_aliases.update(aliases)

    for standard_name, aliases in learned.items():
        if standard_name in repo:
            # 已有标准名 → 并集（仓库在前），只补仓库没有的别名
            extras = [alias for alias in aliases if alias not in known_aliases]
            if extras:
                merged[standard_name] = [*merged[standard_name], *extras]
                known_aliases.update(extras)
            continue
        if standard_name in known_aliases:
            # 该名字在仓库里已是某个标准名的别名 → 跳过（避免反向索引歧义）
            continue
        extras = [alias for alias in aliases if alias not in known_aliases]
        if not extras:
            continue
        merged[standard_name] = extras
        known_aliases.update(extras)
    return merged
