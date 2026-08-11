"""行业向量搜索工具单元测试

覆盖 match_industry_by_keywords 的注册、懒初始化、降级路径。

注意：_openai_client 是模块级懒初始化单例，每个 async 测试前须重置为 None，
避免跨测试状态泄漏（尤其是 RuntimeError mock 污染后续测试）。
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.tools.registry import get_tools

# 模块路径常量（用于 patch）
_NODE_API = "aistock_agent.tools.industry_vector_search.node_api"
_OPENAI_CLIENT = "aistock_agent.tools.industry_vector_search.OpenAI"


# ── 工具注册验证 ──


def test_match_industry_by_keywords_registered_in_event_toolset():
    """match_industry_by_keywords 已注册到 "event" 工具集"""
    tools = get_tools("event")
    tool_names = {t.name for t in tools}
    assert "match_industry_by_keywords" in tool_names, (
        f"event 工具集应包含 match_industry_by_keywords，实际: {tool_names}"
    )


def test_match_industry_by_keywords_has_docstring():
    """工具函数有 docstring（LLM 据此生成参数 schema）"""
    tools = get_tools("event")
    for t in tools:
        if t.name == "match_industry_by_keywords":
            assert t.description is not None
            assert len(t.description) > 20
            return
    pytest.fail("未找到 match_industry_by_keywords 工具")


# ── 懒初始化验证 ──


def test_openai_client_lazy_init_singleton():
    """_get_embedding_client 懒初始化：首次调用创建，再次调用复用"""
    from aistock_agent.tools.industry_vector_search import _get_embedding_client

    mock_client = MagicMock()
    with patch(_OPENAI_CLIENT, return_value=mock_client):
        client1 = _get_embedding_client()
        client2 = _get_embedding_client()

    assert client1 is client2, "懒初始化应返回同一单例"
    assert client1 is mock_client


# ── helper ──


def _make_embedding_mock(embeddings: list | None = None):
    """创建 OpenAI mock，embeddings.create() 返回预设 embedding 列表。

    若 embeddings 为 None，默认返回 1536 维零向量。
    """
    mock_openai = MagicMock()
    mock_response = MagicMock()
    vec = embeddings if embeddings is not None else [0.1] * 1536
    mock_response.data = [MagicMock(embedding=vec)]
    mock_openai.embeddings.create.return_value = mock_response
    return mock_openai


# ── 正常路径 ──


@pytest.mark.asyncio
async def test_match_industry_by_keywords_empty_keywords():
    """空关键词列表 → 返回友好提示（不调 embedding API）"""
    from aistock_agent.tools.industry_vector_search import match_industry_by_keywords

    result = await match_industry_by_keywords.ainvoke({"keywords": []})
    assert "未提供关键词" in result or "无法匹配" in result


@pytest.mark.asyncio
async def test_match_industry_by_keywords_normal_results():
    """正常关键词 → 调用 embedding API → node_api 搜索 → 返回格式化结果"""
    from aistock_agent.tools import industry_vector_search as ivs

    ivs._embedding_client = None
    mock_openai = _make_embedding_mock()
    mock_results = [
        {"name": "新能源汽车", "similarity": 0.95},
        {"name": "动力电池", "similarity": 0.88},
        {"name": "锂矿", "similarity": 0.82},
    ]

    with patch(_OPENAI_CLIENT, return_value=mock_openai):
        with patch(_NODE_API) as mock_node:
            mock_node.semantic_search_industries = AsyncMock(return_value=mock_results)
            result = await ivs.match_industry_by_keywords.ainvoke({
                "keywords": ["新能源汽车", "动力电池", "锂矿"]
            })

    assert "新能源汽车" in result
    assert "0.95" in result
    assert "动力电池" in result
    assert "0.88" in result
    mock_node.semantic_search_industries.assert_called_once()


@pytest.mark.asyncio
async def test_match_industry_by_keywords_single_keyword():
    """单个关键词 → 正常匹配"""
    from aistock_agent.tools import industry_vector_search as ivs

    ivs._embedding_client = None
    mock_openai = _make_embedding_mock()
    mock_results = [{"name": "半导体", "similarity": 0.91}]

    with patch(_OPENAI_CLIENT, return_value=mock_openai):
        with patch(_NODE_API) as mock_node:
            mock_node.semantic_search_industries = AsyncMock(return_value=mock_results)
            result = await ivs.match_industry_by_keywords.ainvoke({"keywords": ["芯片"]})

    assert "半导体" in result
    assert "0.91" in result


# ── 降级路径 ──


@pytest.mark.asyncio
async def test_match_industry_by_keywords_no_results():
    """node_api 返回空列表 → 返回"未找到匹配行业"提示"""
    from aistock_agent.tools import industry_vector_search as ivs

    ivs._embedding_client = None
    mock_openai = _make_embedding_mock()

    with patch(_OPENAI_CLIENT, return_value=mock_openai):
        with patch(_NODE_API) as mock_node:
            mock_node.semantic_search_industries = AsyncMock(return_value=[])
            result = await ivs.match_industry_by_keywords.ainvoke({"keywords": ["火星采矿"]})

    assert "未找到匹配行业" in result


@pytest.mark.asyncio
async def test_match_industry_by_keywords_node_api_error():
    """node_api 抛出异常 → @safe_tool_call 捕获返回降级文本"""
    from aistock_agent.tools import industry_vector_search as ivs

    ivs._embedding_client = None
    mock_openai = _make_embedding_mock()

    with patch(_OPENAI_CLIENT, return_value=mock_openai):
        with patch(_NODE_API) as mock_node:
            mock_node.semantic_search_industries = AsyncMock(
                side_effect=RuntimeError("Connection refused")
            )
            result = await ivs.match_industry_by_keywords.ainvoke({"keywords": ["新能源汽车"]})

    assert "实时连接受限" in result
    assert "模拟分析" in result


@pytest.mark.asyncio
async def test_match_industry_by_keywords_openai_embedding_error():
    """OpenAI embedding API 失败 → @safe_tool_call 捕获返回降级文本"""
    from aistock_agent.tools import industry_vector_search as ivs

    ivs._embedding_client = None

    mock_openai = MagicMock()
    mock_openai.embeddings.create.side_effect = RuntimeError("API quota exceeded")

    with patch(_OPENAI_CLIENT, return_value=mock_openai):
        result = await ivs.match_industry_by_keywords.ainvoke({"keywords": ["新能源汽车"]})

    assert "实时连接受限" in result
    assert "模拟分析" in result


@pytest.mark.asyncio
async def test_match_industry_by_keywords_similarity_zero():
    """相似度为 0 → 仍正常格式化（不崩溃）"""
    from aistock_agent.tools import industry_vector_search as ivs

    ivs._embedding_client = None
    mock_openai = _make_embedding_mock()
    mock_results = [{"name": "某行业", "similarity": 0.0}]

    with patch(_OPENAI_CLIENT, return_value=mock_openai):
        with patch(_NODE_API) as mock_node:
            mock_node.semantic_search_industries = AsyncMock(return_value=mock_results)
            result = await ivs.match_industry_by_keywords.ainvoke({"keywords": ["关键词"]})

    assert "某行业" in result
    assert "0.00" in result


@pytest.mark.asyncio
async def test_match_industry_by_keywords_missing_name_field():
    """返回结果缺 name 字段 → 显示"未知行业"（不崩溃）"""
    from aistock_agent.tools import industry_vector_search as ivs

    ivs._embedding_client = None
    mock_openai = _make_embedding_mock()
    mock_results = [{"similarity": 0.75}]

    with patch(_OPENAI_CLIENT, return_value=mock_openai):
        with patch(_NODE_API) as mock_node:
            mock_node.semantic_search_industries = AsyncMock(return_value=mock_results)
            result = await ivs.match_industry_by_keywords.ainvoke({"keywords": ["测试"]})

    assert "未知行业" in result
    assert "0.75" in result
