"""graph_tools 测试 — 行业知识图谱概念列表与产业链子图"""

from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.graph_tools import get_concepts, get_graph_by_concept


# ── get_concepts ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_concepts_success():
    """get_concepts 正常返回概念列表"""
    mock_data = [
        {"id": "885641.TI", "name": "人工智能", "industryCount": 5},
        {"id": "885666.TI", "name": "半导体", "industryCount": 3},
    ]
    with patch("aistock_agent.tools.graph_tools.node_api") as mock_api:
        mock_api.get_list = AsyncMock(return_value=mock_data)
        result = await get_concepts.ainvoke({})
        assert "人工智能" in result
        assert "半导体" in result
        mock_api.get_list.assert_called_once_with("/internal/graph/concepts")


@pytest.mark.asyncio
async def test_get_concepts_degradation():
    """get_concepts 异常时返回降级文本"""
    with patch("aistock_agent.tools.graph_tools.node_api") as mock_api:
        mock_api.get_list = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_concepts.ainvoke({})
        assert result == DEGRADED_MESSAGE


# ── get_graph_by_concept ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_graph_by_concept_success():
    """get_graph_by_concept 正常返回产业链子图"""
    mock_data = {
        "centerConcept": {"id": "885641.TI", "name": "人工智能"},
        "centerIndustries": [
            {"id": "881101.TI", "name": "半导体", "leadingStocks": [
                {"code": "688981", "name": "中芯国际", "changePct": 3.2}]},
        ],
        "upstreamIndustries": [
            {"id": "881201.TI", "name": "电子化学品", "leadingStocks": []},
        ],
        "downstreamIndustries": [
            {"id": "881301.TI", "name": "计算机设备", "leadingStocks": []},
        ],
        "edges": [
            {"source": "881201.TI", "target": "881101.TI", "confidence": "ai_strong"},
        ],
        "conceptEdges": [
            {"conceptId": "885641.TI", "industryId": "881101.TI", "overlapRatio": 0.85},
        ],
    }
    with patch("aistock_agent.tools.graph_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(return_value=mock_data)
        result = await get_graph_by_concept.ainvoke({"concept": "人工智能"})
        assert "人工智能" in result
        assert "半导体" in result
        assert "中芯国际" in result
        mock_api.get.assert_called_once_with("/internal/graph/人工智能")


@pytest.mark.asyncio
async def test_get_graph_by_concept_degradation():
    """get_graph_by_concept 异常时返回降级文本"""
    with patch("aistock_agent.tools.graph_tools.node_api") as mock_api:
        mock_api.get = AsyncMock(side_effect=RuntimeError("node api down"))
        result = await get_graph_by_concept.ainvoke({"concept": "人工智能"})
        assert result == DEGRADED_MESSAGE
