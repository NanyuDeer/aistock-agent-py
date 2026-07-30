"""播报 Agent 集成测试

覆盖：提示词占位符替换 + 响应提取 + 异常降级
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.agents.workers.broadcast import run

_GET_DEEP_THINK = "aistock_agent.agents.workers.broadcast.get_deep_think"
_NODE_API = "aistock_agent.agents.workers.broadcast.node_api"


def _make_mock_llm(response_content: str) -> MagicMock:
    """构造 mock LLM，ainvoke 返回单个 AIMessage"""
    mock_llm = MagicMock()
    mock_llm.ainvoke = AsyncMock(return_value=AIMessage(content=response_content))
    return mock_llm


@pytest.mark.asyncio
async def test_broadcast_prompt_placeholders_replaced():
    """验证提示词占位符被正确替换"""
    mock_llm = _make_mock_llm("播报内容")
    with patch(_GET_DEEP_THINK, return_value=mock_llm):
        await run({
            "messages": [],
            "analysis_reports": {"morning": "晨报内容", "wind_leader": "风口内容"},
        })

    invoke_args = mock_llm.ainvoke.call_args[0][0]
    system_msg = invoke_args[0]
    assert "晨报内容" in system_msg.content
    assert "风口内容" in system_msg.content


@pytest.mark.asyncio
async def test_broadcast_default_placeholders():
    """验证无分析结果时占位符使用默认值"""
    mock_llm = _make_mock_llm("播报内容")
    with patch(_GET_DEEP_THINK, return_value=mock_llm):
        await run({"messages": [], "analysis_reports": {}})

    invoke_args = mock_llm.ainvoke.call_args[0][0]
    system_msg = invoke_args[0]
    assert "暂无晨报" in system_msg.content
    assert "暂无长线风口分析" in system_msg.content


@pytest.mark.asyncio
async def test_broadcast_response_extraction():
    """验证 final_response 正确提取"""
    expected = "主持人：大家好...分析师：今日风口..."
    mock_llm = _make_mock_llm(expected)
    with patch(_GET_DEEP_THINK, return_value=mock_llm):
        result = await run({
            "messages": [],
            "analysis_reports": {"morning": "晨报", "wind_leader": "风口"},
        })

    assert result["final_response"] == expected


@pytest.mark.asyncio
async def test_broadcast_persists_text_then_triggers_audio():
    """播报稿入库成功后，才调用 Node.js 生成音频。

    契约要求：保存 broadcast.v1 schema（含 dialogue 数组 + source_brief），
    report_type 为 broadcast_{brief_type}，generate-audio 携带 brief_type。
    """
    expected = "主持人：大家好。分析师：今天关注市场主线。"
    mock_llm = _make_mock_llm(expected)
    mock_node_api = MagicMock()
    # 模拟 brief_morning 报告存在
    mock_node_api.get_analysis_report = AsyncMock(return_value={
        "id": 42,
        "content": {
            "as_of": "2026-07-11T00:00:00+08:00",
            "degraded": False,
            "missing_sources": [],
        },
    })
    mock_node_api.save_analysis_report = AsyncMock(return_value={"id": 1})
    mock_node_api.post = AsyncMock(return_value={"audio_path": "/audio.mp3"})

    with (
        patch(_GET_DEEP_THINK, return_value=mock_llm),
        patch(_NODE_API, mock_node_api),
    ):
        result = await run({
            "messages": [],
            "analysis_reports": {},
            "trigger_source": "scheduler",
            "report_date": "2026-07-11",
            "brief_type": "morning",
        })

    # 校验保存的 report_type 和 content schema
    save_call = mock_node_api.save_analysis_report.await_args
    assert save_call.kwargs["report_type"] == "broadcast_morning"
    assert save_call.kwargs["report_date"] == "2026-07-11"
    content = save_call.kwargs["content"]
    assert content["schema_version"] == "broadcast.v1"
    assert content["brief_type"] == "morning"
    assert isinstance(content["dialogue"], list) and len(content["dialogue"]) >= 1
    assert all(d["role"] in ("host", "analyst") for d in content["dialogue"])
    assert content["source_brief"]["id"] == 42
    assert content["source_brief"]["report_type"] == "brief_morning"
    assert content["degraded"] is False
    mock_node_api.post.assert_awaited_once_with(
        "/internal/briefing/generate-audio",
        {"date": "2026-07-11", "brief_type": "morning"},
        timeout=300.0,
    )
    assert result["audio_path"] == "/audio.mp3"


@pytest.mark.asyncio
async def test_broadcast_does_not_generate_audio_when_persistence_fails():
    mock_llm = _make_mock_llm("播报内容")
    mock_node_api = MagicMock()
    mock_node_api.get_analysis_report = AsyncMock(return_value=None)
    mock_node_api.save_analysis_report = AsyncMock(return_value=None)
    mock_node_api.post = AsyncMock()

    with (
        patch(_GET_DEEP_THINK, return_value=mock_llm),
        patch(_NODE_API, mock_node_api),
    ):
        await run({
            "messages": [],
            "analysis_reports": {},
            "trigger_source": "scheduler",
            "report_date": "2026-07-11",
        })

    mock_node_api.save_analysis_report.assert_awaited_once()
    mock_node_api.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_broadcast_exception_degradation():
    """验证 LLM 异常时返回降级文本"""
    with patch(_GET_DEEP_THINK, side_effect=Exception("LLM error")):
        result = await run({"messages": [], "analysis_reports": {}})

    assert result["final_response"] == "播报生成暂时不可用，请稍后重试"
