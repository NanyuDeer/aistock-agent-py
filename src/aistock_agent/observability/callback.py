"""LangChain 回调 handler — token 用量统计 + agent 追踪

通过 callback 机制注入可观测性，不侵入业务逻辑：
- ``TokenUsageCallback``：on_llm_start/on_llm_end 记录 token 用量 → MetricsCollector
- ``AgentTraceCallback``：on_tool_start/end、on_agent_action/finish → structlog 日志

回调 handler 由 ``services/llm.py`` 挂载到 ChatOpenAI 实例（构造时传入 callbacks=），
业务代码（agent 节点、工具函数）完全不感知可观测性的存在。
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from langchain_core.callbacks import BaseCallbackHandler

from aistock_agent.observability.logging import get_logger
from aistock_agent.observability.metrics import (
    MetricsCollector,
    get_metrics_collector,
)
from aistock_agent.services.token_usage import record_token_usage

if TYPE_CHECKING:
    from langchain_core.agents import AgentAction, AgentFinish
    from langchain_core.messages import BaseMessage
    from langchain_core.outputs import LLMResult

logger = get_logger(__name__)


class TokenUsageCallback(BaseCallbackHandler):
    """记录 LLM token 用量。

    - on_llm_start / on_chat_model_start → MetricsCollector.record_llm_start
    - on_llm_end → 提取 response.llm_output.token_usage，累计 prompt/completion/total
    - on_llm_error → MetricsCollector.record_llm_error
    """

    def __init__(self, metrics: MetricsCollector | None = None) -> None:
        # 默认使用全局单例；测试可注入独立实例。
        self._metrics = metrics or get_metrics_collector()

    def on_llm_start(
        self,
        serialized: dict[str, object],
        prompts: list[str],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del parent_run_id, tags, metadata, kwargs
        self._metrics.record_llm_start(_extract_model_name(serialized))

    def on_chat_model_start(
        self,
        serialized: dict[str, object],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        # ChatOpenAI 走 on_chat_model_start（非 on_llm_start）；委托给 on_llm_start
        # 统一处理，避免依赖框架的 NotImplementedError 回退机制（async 路径不一定回退）。
        del messages
        self.on_llm_start(
            serialized,
            [],
            run_id=run_id,
            parent_run_id=parent_run_id,
            tags=tags,
            metadata=metadata,
        )

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        del parent_run_id, kwargs
        usage = _extract_token_usage(response)
        if usage is not None:
            self._metrics.record_llm_tokens(**usage)
            # P10 线 2：同步写入 contextvar 采集层（ws 图任务内累计，synth_answer 收口）
            record_token_usage(
                usage["prompt_tokens"],
                usage["completion_tokens"],
                usage["total_tokens"],
            )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        del parent_run_id, kwargs
        self._metrics.record_llm_error()
        logger.warning("llm_call_error", error=str(error), run_id=str(run_id))


class AgentTraceCallback(BaseCallbackHandler):
    """记录工具调用和 agent 步骤，供追踪。

    - on_tool_start → MetricsCollector.record_tool_start + structlog 日志
    - on_tool_end → structlog 日志
    - on_tool_error → MetricsCollector.record_tool_error + warning 日志
    - on_agent_action / on_agent_finish → structlog 日志
    """

    def __init__(self, metrics: MetricsCollector | None = None) -> None:
        self._metrics = metrics or get_metrics_collector()

    def on_tool_start(
        self,
        serialized: dict[str, object],
        input_str: str,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, object] | None = None,
        inputs: dict[str, object] | None = None,
        **kwargs: object,
    ) -> None:
        del input_str, parent_run_id, tags, metadata, inputs, kwargs
        tool_name = _extract_tool_name(serialized)
        self._metrics.record_tool_start(tool_name)
        logger.info("tool_call_start", tool=tool_name, run_id=str(run_id))

    def on_tool_end(
        self,
        output: object,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        del output, parent_run_id, kwargs
        logger.info("tool_call_end", run_id=str(run_id))

    def on_tool_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        del parent_run_id, kwargs
        self._metrics.record_tool_error()
        logger.warning("tool_call_error", error=str(error), run_id=str(run_id))

    def on_agent_action(
        self,
        action: AgentAction,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        del parent_run_id, kwargs
        logger.info("agent_action", tool=action.tool, run_id=str(run_id))

    def on_agent_finish(
        self,
        finish: AgentFinish,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        **kwargs: object,
    ) -> None:
        del finish, parent_run_id, kwargs
        logger.info("agent_finish", run_id=str(run_id))


def get_default_callbacks() -> list[BaseCallbackHandler]:
    """返回默认可观测性回调列表（TokenUsageCallback + AgentTraceCallback）。

    每次 import 的 handler 共享全局 MetricsCollector 单例。
    """
    return [TokenUsageCallback(), AgentTraceCallback()]


# ── 辅助函数 ──────────────────────────────────────────────────────


def _extract_model_name(serialized: dict[str, object]) -> str:
    """从 serialized 字典提取模型名称。"""
    name = serialized.get("name")
    return str(name) if isinstance(name, str) else ""


def _extract_tool_name(serialized: dict[str, object]) -> str:
    """从 serialized 字典提取工具名称。"""
    name = serialized.get("name")
    return str(name) if isinstance(name, str) else ""


def _extract_token_usage(response: LLMResult) -> dict[str, int] | None:
    """从 LLMResult 提取 token 用量。

    主路径：llm_output.token_usage（graph.ainvoke 路径，llm_output 为 dict）
    Fallback：generations[].message.usage_metadata（graph.astream_events 路径，
    llm_output 为 None 但 ChatGeneration 的 AIMessage 带 usage_metadata）

    Returns:
        含 prompt_tokens/completion_tokens/total_tokens 的字典；
        若无 token_usage 则返回 None。
    """
    # 主路径：llm_output.token_usage（ainvoke 路径）
    llm_output = response.llm_output
    if isinstance(llm_output, dict):
        usage = llm_output.get("token_usage")
        if isinstance(usage, dict):
            return {
                "prompt_tokens": _to_int(usage.get("prompt_tokens", 0)),
                "completion_tokens": _to_int(usage.get("completion_tokens", 0)),
                "total_tokens": _to_int(usage.get("total_tokens", 0)),
            }

    # Fallback：从 generations 提取 AIMessage.usage_metadata（astream_events 路径）
    # astream_events 的 on_llm_end 回调 llm_output 为 None，但 generations 中的
    # ChatGeneration 带 AIMessage，其 usage_metadata 含 input_tokens/output_tokens/total_tokens
    for generation_list in response.generations or []:
        for generation in generation_list:
            message = getattr(generation, "message", None)
            if message is None:
                continue
            usage_metadata = getattr(message, "usage_metadata", None)
            if isinstance(usage_metadata, dict) and usage_metadata:
                return {
                    "prompt_tokens": _to_int(usage_metadata.get("input_tokens", 0)),
                    "completion_tokens": _to_int(usage_metadata.get("output_tokens", 0)),
                    "total_tokens": _to_int(usage_metadata.get("total_tokens", 0)),
                }

    logger.debug(
        "token_usage_extraction_failed",
        llm_output=response.llm_output,
        gen_count=sum(len(g) for g in (response.generations or [])),
    )
    return None


def _to_int(value: object) -> int:
    """将 token 数值安全转为 int（兼容 float / int / None）。"""
    if isinstance(value, bool):
        # bool 是 int 子类，但 token 不应为 bool；按 0 处理避免误计。
        return 0
    if isinstance(value, int | float):
        return int(value)
    return 0
