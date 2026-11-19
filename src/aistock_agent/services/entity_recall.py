"""事件文本 → 板块节点的实体识别召回。

为什么不用向量召回（2026-11-17 实测）：
    用 bge-m3 测两条真实事件，结果暴露了语义匹配的根本局限：

    「台积电10月营收 同比增长59.7%」→ 电器连锁 0.378 / 家用电器 0.376
       模型不知道台积电是半导体公司，只看到「营收增长」就匹配财务类行业
    「日本存储芯片大厂铠侠涨16%」→ 半导体 0.440（低于 0.6 阈值被丢弃）

    前者降阈值也救不了——**模型不懂公司归属**。后者是阈值问题。

所以改成：实体识别 + 查表。
    1. 海外龙头（台积电/铠侠/SK海力士…）→ 板块
    2. 行业关键词（存储芯片/光模块/PCB…）→ 板块
    3. A 股公司名（914 家，来自 stocks 表）→ 行业 → 板块

    三者都是**精确字符串匹配**，每条结果都能说清「因为匹配到了什么」。
    向量召回降级为兜底：实体全没命中时才试。

数据来源与刷新：
    stock_industry_map.json 是从 stocks 表导出的快照（914 家，覆盖 9 个
    与图谱板块相关的行业）。新股上市或行业调整后需重新导出。
    导出命令见 scripts/ 下的说明或 docs。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"


@dataclass(frozen=True)
class EntityHit:
    """一条实体识别结果。"""

    sector: str
    """图谱板块节点名（半导体/存储/光模块/PCB/基建/AI算力/国产替代）。"""

    confidence: float
    """确定性。不是语义相似度——实体识别是精确匹配，这个值反映「归并的可靠性」。"""

    matched_by: str
    """命中依据，如 'overseas:台积电' / 'keyword:存储芯片' / 'stock:北方国际'。"""


@dataclass(frozen=True)
class _EntityData:
    industry_to_sector: dict[str, str]
    keywords_to_sector: dict[str, str]
    overseas_to_sector: dict[str, str]
    stock_to_industry: dict[str, str]
    confidence: dict[str, float]


@lru_cache(maxsize=1)
def _load() -> _EntityData:
    with (_DATA_DIR / "event_sector_map.json").open(encoding="utf-8") as f:
        mapping = json.load(f)
    with (_DATA_DIR / "stock_industry_map.json").open(encoding="utf-8") as f:
        stocks = json.load(f)

    return _EntityData(
        industry_to_sector=mapping["industry_to_sector"],
        keywords_to_sector=mapping["keywords_to_sector"],
        overseas_to_sector=mapping["overseas_to_sector"],
        stock_to_industry=stocks,
        confidence=mapping.get("confidence", {}),
    )


def _longest_hit(text: str, table: dict[str, str]) -> str | None:
    """在文本里找最长的命中键。

    最长优先是必要的：「存储芯片」和「芯片」都在表里时，短键会抢先命中，
    导致「存储」被归成「半导体」。
    """
    best: str | None = None
    for key in table:
        if key and key in text and (best is None or len(key) > len(best)):
            best = key
    return best


def recall_sectors(text: str) -> list[EntityHit]:
    """从事件标题识别板块。返回按确定性降序的命中列表。

    顺序：海外龙头 → 行业关键词 → A 股公司名。
    前两者直接给出板块；公司名要先经 industry 再映射到板块。
    """
    if not text:
        return []

    # 基金/ETF 停牌公告不是产业事件。
    # 实测「全球芯片LOF(501225)将于X月X日停牌」这一模板在 592 条里出现 5 次，
    # 只因含「芯片」二字就被映射到半导体，属于纯噪音。停牌公告一律排除。
    if "停牌" in text:
        return []

    data = _load()
    hits: list[EntityHit] = []

    # 1) 海外龙头：确定性最高——名字就是公司，归属唯一
    key = _longest_hit(text, data.overseas_to_sector)
    if key:
        hits.append(
            EntityHit(
                sector=data.overseas_to_sector[key],
                confidence=data.confidence.get("overseas_to_sector", 0.95),
                matched_by=f"overseas:{key}",
            )
        )

    # 2) 行业关键词：直接映射板块（「存储芯片」在行业分类里不存在，只能这样接）
    key = _longest_hit(text, data.keywords_to_sector)
    if key:
        hits.append(
            EntityHit(
                sector=data.keywords_to_sector[key],
                confidence=data.confidence.get("keywords_to_sector", 0.9),
                matched_by=f"keyword:{key}",
            )
        )

    # 3) A 股公司名：先得行业，再归并到板块
    #    确定性最低——「元器件→半导体」这类归并本身带噪音
    key = _longest_hit(text, data.stock_to_industry)
    if key:
        industry = data.stock_to_industry[key]
        sector = data.industry_to_sector.get(industry)
        if sector:
            hits.append(
                EntityHit(
                    sector=sector,
                    confidence=data.confidence.get("industry_to_sector", 0.8),
                    matched_by=f"stock:{key}->{industry}",
                )
            )

    # 去重：同一板块只保留确定性最高的那条
    deduped: dict[str, EntityHit] = {}
    for hit in hits:
        current = deduped.get(hit.sector)
        if current is None or hit.confidence > current.confidence:
            deduped[hit.sector] = hit

    return sorted(deduped.values(), key=lambda h: h.confidence, reverse=True)
