"""行业向量搜索工具 — 通过 pgvector 语义匹配产业关键词

注册到 "event" 工具集，供 event_agent 在 Step 3（首层行业定位）使用。
"""

from openai import OpenAI
from langchain_core.tools import tool

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call

_openai_client: OpenAI | None = None


def _get_openai_client() -> OpenAI:
    """懒初始化 OpenAI client（避免模块加载时读配置）"""
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI(
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
        )
    return _openai_client


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

    # 1. 拼接关键词生成查询文本
    query_text = " ".join(keywords)

    # 2. 调用 OpenAI embedding API
    openai_client = _get_openai_client()
    response = openai_client.embeddings.create(
        model="text-embedding-3-small",
        input=query_text,
    )
    embedding = response.data[0].embedding  # type: ignore[union-attr]

    # 3. 调用 Node.js pgvector 搜索
    industries = await node_api.semantic_search_industries(
        embedding, threshold=0.7, limit=5
    )

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
