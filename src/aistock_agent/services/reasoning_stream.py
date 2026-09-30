"""通用 reasoning 解说流 — LLM 流式 + 超时 + 静态兜底 + 不计费。

被 chat 链路（graph/nodes/_reasoning.py）与 alert 链路（agents/workers/alert.py）共用。
设计要点：
- timeout_sec 为 None 时**在调用时**读取模块常量 REASONING_TIMEOUT_SEC（便于测试 patch）。
- 任何异常都不向外抛：失败/超时一律发静态兜底文案，绝不阻断主流程。
- LLM 固定 get_quick_think(observe=False) —— 问题 17：旁路 token 不计入用户账单。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aistock_agent.observability.logging import get_logger
from aistock_agent.services.llm import get_quick_think

logger = get_logger(__name__)

REASONING_TIMEOUT_SEC = 2.0


async def stream_reasoning_text(
    sink: Callable[[dict], Awaitable[None]],
    *,
    prompt: str,
    node: str,
    fallback_label: str,
    event_type: str = "reasoning",
    timeout_sec: float | None = None,
) -> None:
    """流式生成解说文本并经 sink 转发。

    Args:
        sink: async 回调，接收一个就绪 payload dict（与传输层解耦）。
        prompt: 已渲染好的完整提示词；空则直接发兜底。
        node: 事件里的节点名（alert 链路恒为阶段名 alert_scan / alert_master）。
        fallback_label: 失败/超时/空 prompt 时发送的静态文案。
        event_type: 事件类型（SSE/WS 共用，默认 "reasoning"）。
        timeout_sec: 首个 chunk 的等待上限；None → 读模块常量 REASONING_TIMEOUT_SEC。
    """
    if not prompt or not prompt.strip():
        await sink({"type": event_type, "node": node, "chunk": fallback_label})
        return

    sec = timeout_sec if timeout_sec is not None else REASONING_TIMEOUT_SEC

    try:
        llm = get_quick_think(observe=False)
        async for chunk in _with_timeout(llm.astream(prompt), sec):
            text = getattr(chunk, "content", None)
            if isinstance(text, str) and text.strip():
                await sink({"type": event_type, "node": node, "chunk": text})
    except TimeoutError:
        logger.warning("reasoning.timeout", node=node)
        await sink({"type": event_type, "node": node, "chunk": fallback_label})
    except Exception:
        logger.warning("reasoning.stream_failed", node=node, exc_info=True)
        await sink({"type": event_type, "node": node, "chunk": fallback_label})


async def _with_timeout(aiter: Any, seconds: float) -> Any:
    """给 async iterator 加逐块超时：循环内对每一块都 wait_for(seconds)。"""
    async def _next() -> Any:
        return await aiter.__anext__()

    while True:
        try:
            chunk = await asyncio.wait_for(_next(), timeout=seconds)
        except StopAsyncIteration:
            return
        yield chunk