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
from urllib.parse import quote

import structlog

from aistock_agent.services.causal_graph import CausalGraph, load_market_graph
from aistock_agent.services.data_client import node_api
from aistock_agent.services.entity_recall import recall_nodes
from aistock_agent.tools.industry_vector_search import semantic_match_industries

logger = structlog.get_logger(__name__)

#: 向量召回阈值。偏松是刻意的——宁可冗余也不漏，假阳性由图谱确认层过滤。
DEFAULT_SIMILARITY_THRESHOLD = 0.6

#: 单次召回上限。多了会让一个事件扇出十几个传导方向。
DEFAULT_RECALL_LIMIT = 5

#: 事件作用域为空时，只接受本身就是板块/题材的节点
# 事件可以映射到任意节点类型。
#
# 2026-11-19 之前这里只有 {"sector", "theme"}，后果是「美联储加息」「人民币贬值」
# 「北向净流出」这类**市场根因**事件全被丢弃——而 M1 要回答的正是「今天为什么这样」，
# 宏观因子本来就是主线，把它们挡在门外是方向性错误。
#
# 放开后仍要求名称精确相等：模糊匹配会把「半导体」塞进「半导体设备」。
_MAPPABLE_KINDS = frozenset({"factor", "market", "sector", "theme", "flow"})


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


def resolve_node_id(graph: CausalGraph, node_key: str) -> str | None:
    """节点名称或 ID → 图谱节点 ID。

    支持两种键：
      - name（「半导体」「美联储政策利率」）——来自行业词/宏观词表
      - id（「SEC_SEMI」「FED_RATE」）——宏观映射表直接用 ID，避免中文名太长

    只匹配 _MAPPABLE_KINDS 里的类型，且要求完全相等。**不做模糊匹配**——
    模糊匹配会把「半导体」塞进「半导体设备」这类错误节点，而错映射比缺映射
    更难排查（下游会沿着错节点一路传导）。
    """
    for node in graph.nodes.values():
        if node.kind not in _MAPPABLE_KINDS:
            continue
        if node.name == node_key or node.id == node_key:
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
      1. 作用域为空 → 直接通过。板块/题材记 direct_sector，
         宏观因子/指数/资金流记 direct_factor（语义不同：后者是根因，不是标的）
      2. 作用域非空 → 要求存在 node → 作用域内某板块 的路径（≤2 跳）

    「路径存在」只说明**可能相关**，不说明确实相关。真正的判定在检验层，
    这里不能越界。
    """
    node = graph.node(node_id)
    if node is None:
        return False, "unknown_node"

    if not scope_sectors:
        # 板块/题材是「被影响的对象」，宏观因子是「施加影响的源头」。
        # 两者都能通过，但下游要能区分——写成同一个 reason 会让人误读。
        if node.kind in ("sector", "theme"):
            return True, "direct_sector"
        return True, "direct_factor"

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

    graph = graph or load_market_graph()
    mappings: list[NodeMapping] = []

    # 2) 实体识别优先。
    #    2026-11-17 实测：向量召回判断不了「台积电属于半导体」（模型不认识公司归属），
    #    也常因阈值丢掉正确结果（「铠侠存储」只拿到 0.44）。实体识别是精确匹配，
    #    每条结果都能说清命中依据，且不依赖模型对公司/产业的认知。
    text = " ".join(keywords)
    for hit in recall_nodes(text):
        node_id = resolve_node_id(graph, hit.node_key)
        if node_id is None:
            continue
        confirmed, reason = confirm_mapping(graph, node_id, scope_sectors)
        mappings.append(
            NodeMapping(
                node_id=node_id,
                similarity=hit.confidence,
                confirmed=confirmed,
                reason=f"{reason}|{hit.matched_by}",
            )
        )

    # 3) 向量召回降级为兜底：只有实体一个都没命中时才试。
    #    失败只记日志不抛——embedding 不可用时不能让整个归因流程崩。
    if not mappings:
        try:
            recalled = await io.recall(keywords, threshold, limit)
        except Exception as error:  # noqa: BLE001 - embedding 失败必须降级
            logger.warning("event_node_recall_failed", event_id=event_id, error=str(error))
            recalled = []

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


class DataClientMappingIO:
    """走 ``data_client`` 的默认 IO 实现。

    读缓存失败（返回 None）时视为未缓存并继续计算——Node 暂时不可用
    不该让映射能力整体失效，只是这一次算不出缓存会写失败（已降级处理）。
    """

    async def read_cache(self, event_id: str) -> dict[str, object]:
        data = await node_api.get(f"/internal/event-node-mappings?eventId={quote(event_id, safe='')}")
        if not data:
            return {"cached": False, "mappings": []}
        mappings = data.get("mappings")
        return {
            "cached": bool(data.get("cached")),
            "mappings": mappings if isinstance(mappings, list) else [],
        }

    async def write_cache(self, event_id: str, mappings: list[dict[str, object]]) -> int:
        data = await node_api.post(
            "/internal/event-node-mappings/upsert",
            {"eventId": event_id, "mappings": mappings},
        )
        if not data:
            return 0
        written = data.get("written")
        return int(written) if isinstance(written, int) else 0

    async def recall(
        self, keywords: list[str], threshold: float, limit: int
    ) -> list[dict[str, object]]:
        return await semantic_match_industries(keywords, threshold=threshold, limit=limit)
