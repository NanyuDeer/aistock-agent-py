"""事件 → 机制图节点映射的计算层。

为什么必须缓存优先（不可协商）：
    向量召回不可复现。同一事件今天匹配「半导体」，回放时可能匹配「消费电子」——
    embedding 服务会失败、模型会换、结果会漂移。2026-08-14 曾因此被迫在回放模式
    短路 embedding（见 event_graph_resolver.py 的 B-2 注释），代价是回放彻底
    拿不到映射。落库 + 缓存优先，才能既保留召回广度又可事后复现。

分工：
    计算在本模块（embedding 与图谱都在 Python 侧），存储走 Node 接口
    （本仓库没有 DB 依赖，也不应为了写一张缓存表而引入）。
    IO 全部通过 MappingIO 注入，便于单测零网络、零 DB。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import structlog

from aistock_agent.services.causal_graph import CausalGraph, load_market_graph

logger = structlog.get_logger(__name__)

#: 向量召回阈值。偏松是刻意的——宁可冗余也不漏，假阳性由图谱确认层过滤。
DEFAULT_SIMILARITY_THRESHOLD = 0.6

#: 单次召回上限。多了会让一个事件扇出十几个传导方向。
DEFAULT_RECALL_LIMIT = 5

#: 事件作用域为空时，只接受本身就是板块/题材的节点
_DIRECT_KINDS = frozenset({"sector", "theme"})


@dataclass(frozen=True)
class NodeMapping:
    """一条「事件 → 节点」候选。"""

    node_id: str
    similarity: float
    confirmed: bool
    reason: str | None = None


class MappingIO(Protocol):
    """映射计算需要的外部 IO。全部由调用方注入。"""

    async def read_cache(self, event_id: str) -> dict[str, object]:
        """读缓存。返回 {"cached": bool, "mappings": [...]}。"""

    async def write_cache(self, event_id: str, mappings: list[dict[str, object]]) -> int:
        """写缓存，返回写入行数。"""

    async def recall(self, keywords: list[str], threshold: float, limit: int) -> list[dict[str, object]]:
        """向量召回，返回 [{"name"/"industry": str, "similarity": float}, ...]。"""


def _recall_name(item: dict[str, object]) -> str:
    """召回结果的行业名。字段名可能是 industry / name，两者都接受。"""
    for key in ("industry", "name", "industry_name"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _recall_similarity(item: dict[str, object]) -> float:
    for key in ("similarity", "score"):
        value = item.get(key)
        if isinstance(value, int | float):
            return float(value)
    return 0.0


def resolve_node_id(graph: CausalGraph, industry_name: str) -> str | None:
    """行业名 → 图谱节点 ID。

    只匹配 sector / theme 节点，且要求名称完全相等。**不做模糊匹配**——
    模糊匹配会把「半导体」塞进「半导体设备」这类错误节点，而错映射比缺映射
    更难排查（下游会沿着错节点一路传导）。
    """
    for node in graph.nodes.values():
        if node.kind in _DIRECT_KINDS and node.name == industry_name:
            return node.id
    return None


def confirm_mapping(
    graph: CausalGraph,
    node_id: str,
    scope_sectors: list[str] | None,
) -> tuple[bool, str]:
    """图谱确认：判断候选节点是否与事件作用域相连。

    返回 (confirmed, reason)。

    规则：
      1. 作用域为空 → 节点本身是板块/题材即通过（事件直接作用于板块）
      2. 作用域非空 → 要求存在 node → 作用域内某板块 的路径（≤2 跳）

    「路径存在」只说明**可能相关**，不说明确实相关。真正的判定在检验层，
    这里不能越界。
    """
    if not scope_sectors:
        return True, "direct_sector"

    node = graph.node(node_id)
    if node is None:
        return False, "unknown_node"

    for sector in scope_sectors:
        target = resolve_node_id(graph, sector)
        if target is None:
            continue
        if graph.path_exists(node_id, target):
            return True, f"path:{node_id}->{target}"

    return False, "no_path_to_scope"


async def get_event_node_mappings(
    event_id: str,
    keywords: list[str],
    *,
    io: MappingIO,
    scope_sectors: list[str] | None = None,
    graph: CausalGraph | None = None,
    threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    limit: int = DEFAULT_RECALL_LIMIT,
) -> list[NodeMapping]:
    """取事件的节点映射。缓存命中直接返回，**不发 embedding**。

    参数
    ----
    event_id
        事件稳定标识。必须来自事件表主键，不能用「标题 + 抓取时间」临时拼——
        标题换个批次就可能变，那样缓存永远命中不了。
    keywords
        用于召回的关键词。
    io
        外部 IO（读缓存 / 写缓存 / 召回），由调用方注入。
    scope_sectors
        事件作用域内的板块名。为空表示事件未解析出作用域。
    """
    if not event_id:
        raise ValueError("event_id must not be empty")

    # 1) 缓存优先。cached=True 时无论 mappings 是否为空都直接返回——
    #    「算过但全被过滤」与「从未算过」是两件事，混用会让回放每次重跑 embedding。
    cached = await io.read_cache(event_id)
    if cached.get("cached"):
        raw = cached.get("mappings")
        items = raw if isinstance(raw, list) else []
        return [
            NodeMapping(
                node_id=str(item.get("nodeId", "")),
                similarity=float(item.get("similarity", 0.0)),
                confirmed=bool(item.get("confirmed", False)),
                reason=item.get("reason") if isinstance(item.get("reason"), str) else None,
            )
            for item in items
            if isinstance(item, dict) and item.get("nodeId")
        ]

    # 2) 向量召回。失败只记日志不抛——embedding 不可用时不能让整个归因流程崩。
    try:
        recalled = await io.recall(keywords, threshold, limit)
    except Exception as error:  # noqa: BLE001 - embedding 失败必须降级
        logger.warning("event_node_recall_failed", event_id=event_id, error=str(error))
        recalled = []

    graph = graph or load_market_graph()

    # 3) 归一 + 确认。归一不到的候选直接丢弃，不硬塞。
    mappings: list[NodeMapping] = []
    for item in recalled:
        name = _recall_name(item)
        if not name:
            continue
        node_id = resolve_node_id(graph, name)
        if node_id is None:
            continue
        similarity = _recall_similarity(item)
        confirmed, reason = confirm_mapping(graph, node_id, scope_sectors)
        mappings.append(
            NodeMapping(
                node_id=node_id,
                similarity=similarity,
                confirmed=confirmed,
                reason=reason,
            )
        )

    # 4) 落库。未确认的也存——冗余不丢，噪音不污染（只在证据矩阵展示）。
    #    落库失败只记日志：存不下不该导致拿不到答案。
    try:
        await io.write_cache(
            event_id,
            [
                {
                    "nodeId": item.node_id,
                    "similarity": item.similarity,
                    "confirmed": item.confirmed,
                    "reason": item.reason,
                }
                for item in mappings
            ],
        )
    except Exception as error:  # noqa: BLE001
        logger.warning("event_node_mapping_store_failed", event_id=event_id, error=str(error))

    return mappings
