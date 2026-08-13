"""节点 reasoning 流式生成器 — 节点 start 时启动，与节点执行并行。

设计：
- 直接接收用户原始 message 字符串（不读 state，因为 initial_state 在
  on_chain_start 时是 stale 的，goal/evidences 尚未填充）。
- 调用 get_quick_think LLM，按节点 prompt 生成 50-100 字思考文本。
- 流式 chunk 通过 WS reasoning 事件转发（不等完整生成）。
- 失败兜底：发送静态 label（_FALLBACK_LABELS），不阻断主流程。
- 超时（默认 2s）：取消并兜底。
"""
from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

from aistock_agent.constants import WSEventType
from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.chat.reasoning import render_reasoning_prompt
from aistock_agent.services.llm import get_quick_think

logger = get_logger(__name__)

# 与 api/ws.py 的 _NODE_LABELS 对齐（避免循环引用，本模块独立维护兜底文案）
_FALLBACK_LABELS: dict[str, str] = {
    "qa_router": "正在理解你的问题",
    "skill_executor": "正在收集证据",
    "synth_answer": "正在综合回答",
    "escalate": "正在深度分析",
    "general_fallback": "正在检索解答",
}

_REASONING_TIMEOUT_SEC = 2.0


async def stream_reasoning(
    sink: Callable[[dict], Awaitable[None]], node: str, message: str
) -> None:
    """异步流式生成 reasoning 文本并通过 sink 转发。

    Args:
        sink: async 回调，接收一个 WS 就绪 payload dict（与连接解耦，
              由调用方决定写入队列/events 列表/直接发送）。
        node: 节点名（qa_router / skill_executor / synth_answer / escalate）。
        message: 用户原始问题字符串。不读 state —— initial_state 在
                 on_chain_start 时是 stale 的。
    - 节点 start 时由 ws.py 通过 asyncio.create_task 启动，不 await。
    - LLM 失败 / 超时 / message 为空 → 发送兜底 label，不抛异常。
    """
    fallback = _FALLBACK_LABELS.get(node, "处理中...")

    if not message or not message.strip():
        await sink({
            "type": WSEventType.REASONING, "node": node, "chunk": fallback,
        })
        return

    try:
        prompt = render_reasoning_prompt(
            node=node, question=message, context={}
        )
    except Exception:
        logger.warning("reasoning.prompt_render_failed", node=node, exc_info=True)
        await sink({
            "type": WSEventType.REASONING, "node": node, "chunk": fallback,
        })
        return

    try:
        # 问题 17：reasoning 旁路 token 不计入用户账单，故不挂计费 callbacks
        llm = get_quick_think(observe=False)
        async for chunk in _with_timeout(llm.astream(prompt), _REASONING_TIMEOUT_SEC):
            text = getattr(chunk, "content", None)
            if isinstance(text, str) and text.strip():
                await sink({
                    "type": WSEventType.REASONING, "node": node, "chunk": text,
                })
    except TimeoutError:
        logger.warning("reasoning.timeout", node=node)
        await sink({
            "type": WSEventType.REASONING, "node": node, "chunk": fallback,
        })
    except Exception:
        logger.warning("reasoning.stream_failed", node=node, exc_info=True)
        await sink({
            "type": WSEventType.REASONING, "node": node, "chunk": fallback,
        })


async def _with_timeout(aiter: Any, seconds: float) -> Any:
    """给 async iterator 加超时（首个 chunk 后不再限制）。"""
    async def _next() -> Any:
        return await aiter.__anext__()

    while True:
        try:
            chunk = await asyncio.wait_for(_next(), timeout=seconds)
        except StopAsyncIteration:
            return
        yield chunk
