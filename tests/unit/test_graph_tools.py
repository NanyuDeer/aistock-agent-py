"""graph_tools 测试 — 行业知识图谱概念列表与产业链子图"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.services.data_client import IndustryChainReadResult
from aistock_agent.tools.base import DEGRADED_MESSAGE
from aistock_agent.tools.graph_tools import (
    get_concepts,
    get_graph_by_concept,
    get_industry_chain,
)
from aistock_agent.tools.registry import get_exposed_skills, get_tools

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


# ── get_industry_chain ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_industry_chain_returns_found_one_hop_evidence_json():
    """成功结果保留 Node 提供的一跳图谱事实。"""
    mock_data = {
        "industry": {"id": "881101.TI", "name": "动力电池"},
        "upstream": [
            {
                "id": "881201.TI",
                "name": "锂矿",
                "leadingStocks": [
                    {"code": "002466", "name": "天齐锂业"},
                    {"code": "002460", "name": "赣锋锂业"},
                ],
            }
        ],
        "downstream": [
            {
                "id": "881301.TI",
                "name": "新能源汽车",
                "leadingStocks": [
                    {"code": "300750", "name": "宁德时代"},
                    {"code": "002594", "name": "比亚迪"},
                    {"code": "601633", "name": "长城汽车"},
                    {"code": "000625", "name": "长安汽车"},
                ],
            }
        ],
        "graphVersion": "kg-2026-07-22",
        "updatedAt": "2026-07-22T09:00:00Z",
    }
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(
            return_value=IndustryChainReadResult(
                "found",
                mock_data,
                "IndustryKGService",
            )
        ),
    ) as mock_get_industry_chain:
        result = await get_industry_chain.ainvoke({"industry_name": " 动力电池 "})

    payload = json.loads(result)
    assert payload["status"] == "found"
    assert payload["degraded"] is False
    assert payload["scope"] == "one_hop"
    assert payload["source"] == "IndustryKGService"
    assert payload["industry"]["id"] == "881101.TI"
    assert payload["upstream"][0]["id"] == "881201.TI"
    assert payload["downstream"][0]["id"] == "881301.TI"
    assert payload["downstream"][0]["leadingStocks"][3]["code"] == "000625"
    assert payload["graphVersion"] == "kg-2026-07-22"
    assert payload["updatedAt"] == "2026-07-22T09:00:00Z"
    assert payload["missingBoundary"] is None
    mock_get_industry_chain.assert_awaited_once_with("动力电池")


@pytest.mark.asyncio
async def test_get_industry_chain_empty_name_degrades_without_request():
    """空白行业名给出可审计的输入降级，不请求 Node。"""
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(),
    ) as mock_get_industry_chain:
        result = await get_industry_chain.ainvoke({"industry_name": "   "})

    payload = json.loads(result)
    assert payload["status"] == "invalid_input"
    assert payload["degraded"] is True
    assert payload["source"] is None
    assert "本次未取得 IndustryKG 图谱事实" in payload["missingBoundary"]
    mock_get_industry_chain.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_industry_chain_request_exception_degrades_as_request_failed():
    """专用客户端意外抛错时保留可审计的请求失败状态。"""
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(side_effect=RuntimeError("node api down")),
    ) as mock_get_industry_chain:
        result = await get_industry_chain.ainvoke({"industry_name": "光伏组件"})

    payload = json.loads(result)
    assert payload["status"] == "request_failed"
    assert payload["degraded"] is True
    assert payload["source"] is None
    assert "本次未取得 IndustryKG 图谱事实" in payload["missingBoundary"]
    mock_get_industry_chain.assert_awaited_once_with("光伏组件")


@pytest.mark.asyncio
async def test_get_industry_chain_malformed_client_result_degrades_as_invalid_response():
    """客户端返回非结果对象时仍返回带边界的 JSON，不能退化为通用文本。"""
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(return_value=object()),
    ):
        result = await get_industry_chain.ainvoke({"industry_name": "光伏组件"})

    payload = json.loads(result)
    assert payload["status"] == "invalid_response"
    assert payload["degraded"] is True
    assert payload["source"] is None
    assert "本次未取得 IndustryKG 图谱事实" in payload["missingBoundary"]


@pytest.mark.asyncio
async def test_get_industry_chain_rejects_found_result_from_other_source():
    """found 结果必须来自 IndustryKGService，不能接受其他来源。"""
    valid_looking_data = {
        "industry": {"id": "881101.TI", "name": "动力电池"},
        "upstream": [],
        "downstream": [],
    }
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(
            return_value=IndustryChainReadResult(
                "found", valid_looking_data, "OtherService"
            )
        ),
    ):
        result = await get_industry_chain.ainvoke({"industry_name": "动力电池"})

    payload = json.loads(result)
    assert payload["status"] == "invalid_response"
    assert payload["degraded"] is True
    assert payload["missingBoundary"]


@pytest.mark.asyncio
async def test_get_industry_chain_rejects_found_result_with_malformed_nodes():
    """found 结果的关联节点缺少基本图谱结构时必须降级。"""
    malformed_data = {
        "industry": {"id": "881101.TI", "name": "动力电池"},
        "upstream": [{"id": "881201.TI", "name": "锂矿"}],
        "downstream": [],
    }
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(
            return_value=IndustryChainReadResult(
                "found", malformed_data, "IndustryKGService"
            )
        ),
    ):
        result = await get_industry_chain.ainvoke({"industry_name": "动力电池"})

    payload = json.loads(result)
    assert payload["status"] == "invalid_response"
    assert payload["degraded"] is True
    assert payload["missingBoundary"]


@pytest.mark.asyncio
async def test_get_industry_chain_rejects_unknown_result_status():
    """客户端返回未知状态时必须归类为无效响应。"""
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(return_value=IndustryChainReadResult("unknown")),  # type: ignore[arg-type]
    ):
        result = await get_industry_chain.ainvoke({"industry_name": "动力电池"})

    payload = json.loads(result)
    assert payload["status"] == "invalid_response"
    assert payload["degraded"] is True
    assert payload["missingBoundary"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [
        "not_found",
        "authentication_failed",
        "upstream_failed",
        "timeout",
        "invalid_response",
    ],
)
async def test_get_industry_chain_preserves_degraded_status_boundary(status: str):
    """Node 读取失败保留状态，不伪造来源或关系事实。"""
    with patch(
        "aistock_agent.tools.graph_tools.node_api.get_industry_chain",
        new=AsyncMock(return_value=IndustryChainReadResult(status)),
    ):
        result = await get_industry_chain.ainvoke({"industry_name": "光伏组件"})

    payload = json.loads(result)
    assert payload["status"] == status
    assert payload["degraded"] is True
    assert payload["scope"] == "one_hop"
    assert payload["source"] is None
    assert payload["industry"] is None
    assert payload["upstream"] is None
    assert payload["downstream"] is None
    assert "本次未取得 IndustryKG 图谱事实" in payload["missingBoundary"]


def test_get_industry_chain_is_event_only_and_not_exposed():
    """行业图谱工具只提供给 event Agent，不暴露到通用技能接口。"""
    event_tool_names = {tool.name for tool in get_tools("event")}
    general_tool_names = {tool.name for tool in get_tools("general")}
    exposed_tool_names = {tool.name for tool in get_exposed_skills()}

    assert "get_industry_chain" in event_tool_names
    assert "get_industry_chain" not in general_tool_names
    assert "get_industry_chain" not in exposed_tool_names
