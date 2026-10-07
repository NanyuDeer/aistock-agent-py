"""services.reasoning_stream 单测 — 流式 / 兜底 / 超时 / 不计费。"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.services.reasoning_stream import stream_reasoning_text
from aistock_agent.services.token_usage import get_token_usage, reset_token_usage


@pytest.mark.asyncio
async def test_sends_chunks_with_custom_node_and_type():
    """LLM 正常流式 → 按 (node, event_type) 发送事件序列。"""
    sink = AsyncMock()

    async def fake_astream(*_args, **_kwargs):
        for chunk in ["我在核对", "资讯与盘口"]:
            yield MagicMock(content=chunk)

    with patch("aistock_agent.services.reasoning_stream.get_quick_think") as mock_llm:
        mock_llm.return_value.astream = fake_astream
        await stream_reasoning_text(
            sink, prompt="PROMPT", node="alert_scan", fallback_label="兜底文案"
        )

    assert sink.await_count == 2
    first = sink.await_args_list[0].args[0]
    assert first == {"type": "reasoning", "node": "alert_scan", "chunk": "我在核对"}


@pytest.mark.asyncio
async def test_empty_prompt_uses_fallback_without_llm():
    """prompt 为空 → 不调 LLM，直接发兜底。"""
    sink = AsyncMock()
    with patch("aistock_agent.services.reasoning_stream.get_quick_think") as mock_llm:
        await stream_reasoning_text(
            sink, prompt="", node="alert_scan", fallback_label="兜底文案"
        )
    mock_llm.assert_not_called()
    assert sink.await_args_list[0].args[0]["chunk"] == "兜底文案"


@pytest.mark.asyncio
async def test_llm_failure_falls_back():
    """LLM 抛异常 → 发兜底，不抛异常。"""
    sink = AsyncMock()
    with patch("aistock_agent.services.reasoning_stream.get_quick_think") as mock_llm:
        mock_llm.side_effect = RuntimeError("LLM down")
        await stream_reasoning_text(
            sink, prompt="PROMPT", node="alert_master", fallback_label="兜底文案"
        )
    assert sink.await_args_list[-1].args[0] == {
        "type": "reasoning", "node": "alert_master", "chunk": "兜底文案",
    }


@pytest.mark.asyncio
async def test_timeout_falls_back():
    """超时 → 取消并兜底（timeout 通过模块常量 patch 注入）。"""
    sink = AsyncMock()

    async def slow_astream(*_args, **_kwargs):
        await asyncio.sleep(5)
        yield MagicMock(content="never")  # type: ignore[unreachable]

    with patch("aistock_agent.services.reasoning_stream.get_quick_think") as mock_llm, \
         patch("aistock_agent.services.reasoning_stream.REASONING_TIMEOUT_SEC", 0.1):
        mock_llm.return_value.astream = slow_astream
        await stream_reasoning_text(
            sink, prompt="PROMPT", node="alert_scan", fallback_label="兜底文案"
        )
    assert sink.await_args_list[-1].args[0]["chunk"] == "兜底文案"


@pytest.mark.asyncio
async def test_not_billed():
    """旁路解说以 observe=False 调用，token 不进用户账单。"""
    sink = AsyncMock()

    async def fake_astream(*_args, **_kwargs):
        yield MagicMock(content="我在分析")

    with patch("aistock_agent.services.reasoning_stream.get_quick_think") as mock_llm:
        mock_llm.return_value.astream = fake_astream
        reset_token_usage()
        await stream_reasoning_text(
            sink, prompt="PROMPT", node="alert_scan", fallback_label="兜底文案"
        )
    mock_llm.assert_called_once_with(observe=False)
    assert get_token_usage() is None