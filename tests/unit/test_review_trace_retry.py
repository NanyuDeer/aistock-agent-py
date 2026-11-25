"""review LLM 输出校验失败重试一次（2026-08-18 修复）单测。

线上 2026-08-18 复盘连续失败：
- 15:30 review_quick：LLM 返回空串 → JSON EOF → 整份降级
- 16:25/16:27 手动补跑：event_hits[].result='partial' 超出枚举 → 整份降级

修复 2：_generate_trace_with_retry 在 LLM 输出解析/校验失败时重试一次，
防止单次 LLM 输出抖动直接拖垮整份复盘报告（对齐晨报降级重试先例）。
"""

import json
from unittest.mock import AsyncMock

import pytest
from langchain_core.messages import AIMessage

from aistock_agent.agents.workers.review import _generate_trace_with_retry


def _minimal_trace_json() -> str:
    """构造能通过 model_validate_json 的最小 MarketTraceResult JSON。"""
    return json.dumps({
        "schema_version": "1.1",
        "attribution_status": "insufficient",
        "candidates": [],
        "primary_chain_id": None,
        "alternative_chain_id": None,
        "confidence": "low",
        "unresolved_questions": [],
    })


@pytest.mark.asyncio
async def test_generate_trace_with_retry_succeeds_on_second_attempt():
    """首次 LLM 输出非法（空串），第二次合法 → 重试一次并返回合法 trace。

    复现 2026-08-18 15:30 review_quick 失败：LLM 空响应 → JSON EOF → 整份降级。
    """
    llm = AsyncMock()
    llm.ainvoke.side_effect = [
        AIMessage(content=""),
        AIMessage(content=_minimal_trace_json()),
    ]

    trace = await _generate_trace_with_retry(llm, [])

    assert llm.ainvoke.await_count == 2
    assert trace.attribution_status == "insufficient"


@pytest.mark.asyncio
async def test_generate_trace_with_retry_raises_after_two_failures():
    """两次均输出非法 JSON → 抛出最后一次异常（由调用方降级为不可用）。"""
    llm = AsyncMock()
    llm.ainvoke.side_effect = [
        AIMessage(content=""),
        AIMessage(content="not json"),
    ]

    with pytest.raises(Exception):
        await _generate_trace_with_retry(llm, [])

    assert llm.ainvoke.await_count == 2
