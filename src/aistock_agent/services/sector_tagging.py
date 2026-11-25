"""板块标签（板块消息 → 关联个股）纯规则实现。

背景：统一事件库中的板块级新闻既没有 ``symbol``、采集器也从不填 ``industry``，
导致 stock_trace 的 sector 证据域只能引用板块涨跌幅事实，无法回答"板块为什么动"。
本模块用人工维护的别名词典（``sector_aliases_store.load_merged_aliases``）给事件
打「标准板块名」标签，并把个股所属板块名归一到同一口径，供按板块召回事件。

设计约束：
- **纯规则**：无 LLM、无 DB、无网络；词典缺失/损坏时安全退化为空结果。
- **只输出标准板块名**（不是命中到的原始别名），保证打标侧与求交侧口径一致
  （例：别名「贵金属」同时属于黄金/白银 → 输出两个标准名）。
- 标准名常为「/」连接的复合名（如「AI/光模块/半导体」），其分段也作为匹配项，
  这样 ``stock_concept_mapping`` 里的板块名（如「光模块」）也能归一。

注意：这里只做"这个事件像不像板块消息"的召回，不判断涨跌方向，也不替代
LLM 的归因判断；误召回由归因层按证据强度自行取舍。
"""

from __future__ import annotations

from pathlib import Path

from aistock_agent.services.sector_aliases_store import (
    LEARNED_ALIASES_FILE,
    REPO_ALIASES_FILE,
    load_merged_aliases,
)

# 单条事件最多保留的标准名数量：别名词典存在重叠（同一别名属多个标准名），
# 上限防止一条泛化标题打满标签、污染匹配；8 足够覆盖「5G」这类多归属词。
MAX_SECTOR_TAGS = 8
_MIN_TERM_LENGTH = 2

# 宽基指数 / 两融 / 互联互通：这类"板块"几乎覆盖全市场，与个股异动无因果关联，
# 既不作为个股所属板块参与召回，也不生成板块证据（2026-10-17 江淮汽车验证发现
# 「上证180成份股」会把 180 只成分股的公告全召回成板块消息）。
_BROAD_SECTOR_MARKERS = ("成份股", "成分股")
_BROAD_SECTOR_NAMES = frozenset({
    "上证50", "上证180", "沪深300", "中证100", "中证500", "中证1000",
    "深证100", "创业板指", "科创50", "融资融券", "转融券标的",
    "沪股通", "深股通", "港股通", "MSCI中国", "富时罗素", "标普道琼斯A股",
})

# 跨概念大扩张黑名单：这些名称只代表自身板块，不再展开到其所属的伞形标准名。
# 例：DeepSeek概念 同时是「AI/算力/半导体」「AI应用/办公软件」的别名，若参与
# 扩张，一只概念股会把所有 AI/宏观新闻（含"人工智能"）都召回成板块消息。
_NO_EXPANSION_NAMES = frozenset({"DeepSeek概念"})


def is_broad_sector_name(name: str) -> bool:
    """是否为宽基指数/两融/互联互通板块（宽泛到无法解释个股异动）。"""
    trimmed = (name or "").strip()
    if not trimmed:
        return False
    if trimmed in _BROAD_SECTOR_NAMES:
        return True
    return any(marker in trimmed for marker in _BROAD_SECTOR_MARKERS)


# 索引缓存：按两个别名文件的 (mtime, size) 失效。
# 不能用 lru_cache 永久缓存——运行时 learned 字典由 snapshot_builder 每日回写，
# 长期驻留进程（scheduler）若永久缓存，新学到的别名要等重启才生效。
_cached_signature: tuple[float, int, float, int] | None = None
_cached_index: dict[str, frozenset[str]] = {}


def _is_matchable_term(term: str) -> bool:
    """过滤过短/过于泛化的匹配项。

    纯 ASCII 两字母词（``AI`` / ``IT``）在任何财经文本里都会命中，收益低而噪声大；
    含数字的两字符词（``5G``）是有效板块词，保留。
    """
    if len(term) < _MIN_TERM_LENGTH:
        return False
    if all(ord(ch) < 128 for ch in term):
        return not (len(term) == _MIN_TERM_LENGTH and not any(ch.isdigit() for ch in term))
    return True


def _file_signature(path: Path) -> tuple[float, int]:
    """文件签名（缺失/不可读按 0 处理，不抛错）。"""
    try:
        stat = path.stat()
        return (stat.st_mtime, stat.st_size)
    except OSError:
        return (0.0, 0)


def _term_to_standards() -> dict[str, frozenset[str]]:
    """构建「标准名/别名/标准名分段 → 标准名集合」反向索引（文件变更时重建）。"""
    global _cached_signature, _cached_index
    signature = (*_file_signature(REPO_ALIASES_FILE), *_file_signature(LEARNED_ALIASES_FILE))
    if signature == _cached_signature:
        return _cached_index

    mapping: dict[str, set[str]] = {}
    for standard_name, aliases in load_merged_aliases().items():
        if not isinstance(standard_name, str) or not standard_name.strip():
            continue
        standard = standard_name.strip()
        terms: set[str] = {standard}
        terms.update(alias.strip() for alias in aliases if isinstance(alias, str))
        terms.update(part.strip() for part in standard.split("/"))
        for term in terms:
            if _is_matchable_term(term):
                mapping.setdefault(term, set()).add(standard)
    _cached_index = {term: frozenset(standards) for term, standards in mapping.items()}
    _cached_signature = signature
    return _cached_index


def tag_sectors(text: str) -> list[str]:
    """返回文本命中的标准板块名（去重、字典序稳定、最多 ``MAX_SECTOR_TAGS`` 个）。"""
    if not text or not text.strip():
        return []
    index = _term_to_standards()
    if not index:
        return []
    tags: set[str] = set()
    for term, standards in index.items():
        if term in text:
            tags.update(standards)
    return sorted(tags)[:MAX_SECTOR_TAGS]


def expand_sector_names(names: list[str]) -> set[str]:
    """把板块名归一为「自身 + 其所属标准名」集合（未收录的名称退化为自身）。

    两类名称不参与归一，只保留自身：
    - 宽基/两融/互联互通板块（``is_broad_sector_name``）——召回结果全是噪声；
    - 跨概念大扩张黑名单（``_NO_EXPANSION_NAMES``）——避免一只概念股召回
      全部伞形板块新闻。
    """
    index = _term_to_standards()
    expanded: set[str] = set()
    for name in names:
        trimmed = (name or "").strip()
        if not trimmed or is_broad_sector_name(trimmed):
            continue
        expanded.add(trimmed)
        if trimmed in _NO_EXPANSION_NAMES:
            continue
        for part in trimmed.split("/"):
            part = part.strip()
            if part:
                expanded.update(index.get(part, frozenset()))
    return expanded
