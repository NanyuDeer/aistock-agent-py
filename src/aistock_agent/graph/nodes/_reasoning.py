"""节点 reasoning 流式生成器 — 节点 start 时启动，与节点执行并行。

本模块只负责"节点名 → prompt"的映射与 chat 专用兜底文案；
真正的 LLM 流式 / 超时 / 兜底逻辑在 services/reasoning_stream.py 共用。
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

from aistock_agent.observability.logging import get_logger
from aistock_agent.prompts.chat.reasoning import render_reasoning_prompt
from aistock_agent.services.reasoning_stream import stream_reasoning_text

logger = get_logger(__name__)

# 与 api/ws.py 的 _NODE_LABELS 对齐（避免循环引用，本模块独立维护兜底文案）
_FALLBACK_LABELS: dict[str, str] = {
    "qa_router": "正在理解你的问题",
    "skill_executor": "正在收集证据",
    "synth_answer": "正在综合回答",
    "escalate": "正在深度分析",
    "general_fallback": "正在检索解答",
}


async def stream_reasoning(
    sink: Callable[[dict[str, object]], Awaitable[None]], node: str, message: str
) -> None:
    """异步流式生成 reasoning 文本并通过 sink 转发（签名与行为保持不变）。"""
    fallback = _FALLBACK_LABELS.get(node, "处理中...")

    if not message or not message.strip():
        await stream_reasoning_text(
            sink, prompt="", node=node, fallback_label=fallback
        )
        return

    try:
        prompt = render_reasoning_prompt(node=node, question=message, context={})
    except Exception:
        logger.warning("reasoning.prompt_render_failed", node=node, exc_info=True)
        await stream_reasoning_text(
            sink, prompt="", node=node, fallback_label=fallback
        )
        return

    await stream_reasoning_text(
        sink, prompt=prompt, node=node, fallback_label=fallback
    )