"""行业向量搜索 — 通过 pgvector 语义匹配产业关键词

提供：
- ``semantic_match_industries``：可复用纯函数（供 event_graph_resolver 语义 fallback 与工具共用）。
- ``match_industry_by_keywords``：@tool，供 LLM ReAct 使用（当前 prompt 已禁止主动调用，保留兼容）。

embedding 使用独立配置（settings.embedding_*），未配置时仅 fallback 到 openai_*（测试用），
生产必须显式配置支持 embedding 的服务端点（禁用 LLM 端点，对齐硬约束）。
"""

import structlog
from langchain_core.tools import tool
from openai import OpenAI

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call

logger = structlog.get_logger()

_embedding_client: OpenAI | None = None


def _get_embedding_client() -> OpenAI:
    """懒初始化 embedding OpenAI client（独立配置优先，fallback openai_* 供测试）。"""
    global _embedding_client
    if _embedding_client is None:
        _embedding_client = OpenAI(
            api_key=settings.embedding_api_key or settings.openai_api_key,
            base_url=settings.embedding_base_url or settings.openai_base_url,
        )
    return _embedding_client


async def semantic_match_industries(
    keywords: list[str],
    threshold: float = 0.7,
    limit: int = 5,
) -> list[dict[str, object]]:
    """语义匹配行业：关键词 → embedding → pgvector 搜索。

    供 event_graph_resolver 语义 fallback 与 match_industry_by_keywords 工具复用。

    Args:
        keywords: 产业关键词列表，如 ["光伏", "太阳能"]
        threshold: 相似度阈值（0-1），默认 0.7
        limit: 返回数量上限，默认 5

    Returns:
        匹配行业记录列表 [{name, similarity, ...}]。

    Raises:
        Exception: embedding / 搜索失败时向上抛，由调用方处理
            （工具侧 @safe_tool_call 捕获降级；resolver fallback 侧
            _try_semantic_fallback 捕获返回 None）。
    """
    if not keywords:
        return []

    # B-2（2026-08-14）：回放模式显式短路——不发 embedding、不调 Node pgvector
    # 搜索（裁决书 B 论题"embedding/语义 fallback 短路"；industry_vector_search
    # 直连 embedding API，不在 replay_layer 服务清单内，须在入口拦截）。
    from aistock_agent.iterate.replay_layer import is_replay_mode

    if is_replay_mode():
        logger.info("industry_vector_search_skipped_replay")
        return []

    # 无 embedding 凭据时快速短路（避免无效网络请求；生产未配置 EMBEDDING_* 时 fallback 安全降级）
    if not (settings.embedding_api_key or settings.openai_api_key):
        logger.warning("semantic_match_industries_no_credentials")
        return []

    # 1. 拼接关键词生成查询文本
    query_text = " ".join(keywords)

    # 2. 调用 embedding API（独立 EMBEDDING_* 配置）
    openai_client = _get_embedding_client()
    response = openai_client.embeddings.create(
        model=settings.embedding_model,
        input=query_text,
    )
    embedding = response.data[0].embedding  # type: ignore[union-attr]

    # 3. 调用 Node.js pgvector 搜索
    industries = await node_api.semantic_search_industries(
        embedding, threshold=threshold, limit=limit
    )

    return industries


@tool
@safe_tool_call
async def match_industry_by_keywords(keywords: list[str]) -> str:
    """根据产业关键词，在行业嵌入向量库中做语义匹配，返回前 5 个最相关的行业。

    用于事件传导分析 Step 3（首层行业定位）：将新闻中提取的产业实体关键词
    映射到项目已有的行业数据库，确保 Agent 输出的行业一定来自现有行业库。

    Args:
        keywords: 从新闻中提取的产业关键词列表，如 ["新能源汽车", "动力电池", "锂矿"]

    Returns:
        匹配行业列表，每行格式：行业名 (相似度: 0.92)
        无匹配时返回"未找到匹配行业，请尝试调整关键词"
    """
    if not keywords:
        return "未提供关键词，无法匹配行业"

    industries = await semantic_match_industries(keywords, threshold=0.7, limit=5)

    if not industries:
        return "未找到匹配行业，请尝试调整关键词"

    lines: list[str] = []
    for ind in industries:
        name = str(ind.get("name", "未知行业"))
        similarity = float(str(ind.get("similarity", 0)))
        lines.append(f"- {name} (相似度: {similarity:.2f})")

    return "\n".join(lines)


# ── 自注册到 Tool Registry ──
from aistock_agent.tools.registry import register  # noqa: E402

register("event", match_industry_by_keywords)
