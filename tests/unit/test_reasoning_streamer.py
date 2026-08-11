"""reasoning streamer 单测 — LLM 流式 + 失败兜底 + 超时。

关键：stream_reasoning(sink, node, message: str) 直接接收用户原始问题字符串，
不读 state。测试通过断言 render_reasoning_prompt 收到非空 message 来验证。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.graph.nodes._reasoning import stream_reasoning


@pytest.mark.asyncio
async def test_stream_reasoning_sends_chunks():
    """LLM 正常流式 → 发送 reasoning 事件序列。"""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    async def fake_astream(*_args, **_kwargs):
        for chunk in ["我先拆", "问题拆解", "为两步"]:
            yield MagicMock(content=chunk)

    with patch("aistock_agent.graph.nodes._reasoning.get_quick_think") as mock_llm, \
         patch("aistock_agent.graph.nodes._reasoning.render_reasoning_prompt") as mock_render:
        mock_render.return_value = "prompt"
        mock_llm.return_value.astream = fake_astream
        # 直接传 message 字符串，不传 state；sink 传 ws.send_json
        await stream_reasoning(ws.send_json, "qa_router", "查 600519 的行情")

    # 验证 render_reasoning_prompt 收到非空 question
    mock_render.assert_called_once()
    call_kwargs = mock_render.call_args.kwargs
    assert call_kwargs["node"] == "qa_router"
    assert call_kwargs["question"] == "查 600519 的行情"
    # 验证发送了 reasoning 事件序列
    assert ws.send_json.await_count >= 3
    first_call = ws.send_json.await_args_list[0].args[0]
    assert first_call["type"] == "reasoning"
    assert first_call["node"] == "qa_router"
    assert first_call["chunk"] == "我先拆"


@pytest.mark.asyncio
async def test_stream_reasoning_llm_failure_falls_back_to_label():
    """LLM 异常 → 发送静态 label 兜底，不抛异常。"""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    with (
        patch("aistock_agent.graph.nodes._reasoning.get_quick_think") as mock_llm,
        patch(
            "aistock_agent.graph.nodes._reasoning.render_reasoning_prompt",
            return_value="prompt",
        ),
    ):
        mock_llm.side_effect = RuntimeError("LLM down")
        await stream_reasoning(ws.send_json, "qa_router", "查 600519 的行情")

    # 至少发一个 reasoning 事件（兜底 label）
    assert ws.send_json.await_count >= 1
    fallback_call = ws.send_json.await_args_list[-1].args[0]
    assert fallback_call["type"] == "reasoning"
    assert fallback_call["node"] == "qa_router"
    # 兜底 label 应是 _FALLBACK_LABELS["qa_router"] 的值
    assert "理解" in fallback_call["chunk"] or "处理中" in fallback_call["chunk"]


@pytest.mark.asyncio
async def test_stream_reasoning_timeout_falls_back():
    """超时（>2s）→ 取消并兜底。"""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    async def slow_astream(*_args, **_kwargs):
        await asyncio.sleep(5)
        yield MagicMock(content="never")  # type: ignore[unreachable]

    with (
        patch("aistock_agent.graph.nodes._reasoning.get_quick_think") as mock_llm,
        patch(
            "aistock_agent.graph.nodes._reasoning.render_reasoning_prompt",
            return_value="prompt",
        ),
    ):
        mock_llm.return_value.astream = slow_astream
        # 把超时缩短到 0.1s 加速测试
        with patch("aistock_agent.graph.nodes._reasoning._REASONING_TIMEOUT_SEC", 0.1):
            await stream_reasoning(ws.send_json, "qa_router", "查 600519 的行情")

    assert ws.send_json.await_count >= 1


@pytest.mark.asyncio
async def test_stream_reasoning_empty_message_uses_fallback():
    """message 为空 → 不调 LLM，直接发兜底 label。"""
    ws = MagicMock()
    ws.send_json = AsyncMock()

    with patch("aistock_agent.graph.nodes._reasoning.get_quick_think") as mock_llm:
        await stream_reasoning(ws.send_json, "qa_router", "")

    # 不应调用 LLM
    mock_llm.assert_not_called()
    # 应发兜底 label
    assert ws.send_json.await_count == 1
    call = ws.send_json.await_args_list[0].args[0]
    assert call["type"] == "reasoning"
    assert call["node"] == "qa_router"
