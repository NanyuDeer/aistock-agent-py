"""双模型工厂 — quick_think / deep_think

根据用途选择不同模型：
- quick_think：意图分类/路由，低延迟低成本
- deep_think：深度分析/晨报/事件，推理质量优先

可观测性：通过 ``callbacks=`` 挂载 TokenUsageCallback / AgentTraceCallback，
不侵入业务逻辑（agent 节点 / 工具函数不感知回调存在）；
若开启 ``langsmith_enabled`` 则设置 LangChain 追踪环境变量。
"""

import os
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.runnables import Runnable
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, SecretStr

from aistock_agent.config import settings
from aistock_agent.observability.callback import get_default_callbacks


def _setup_langsmith_tracing() -> None:
    """若启用 LangSmith，设置 LangChain 追踪环境变量。

    LangChain 在回调管理器初始化时读取这些环境变量，自动注入 LangChainTracer。
    幂等：使用 setdefault，不覆盖已有值。默认关闭（langsmith_enabled=False），
    仅在需要调试/追踪时通过环境变量开启。
    """
    if not settings.langsmith_enabled or not settings.langsmith_api_key:
        return
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", settings.langsmith_api_key)
    os.environ.setdefault("LANGCHAIN_PROJECT", settings.langsmith_project)


# 模块加载时一次性配置 LangSmith（幂等，默认 no-op）
_setup_langsmith_tracing()


def _get_observability_callbacks() -> list[BaseCallbackHandler]:
    """返回可观测性回调列表（token 用量统计 + agent 追踪）。"""
    return get_default_callbacks()


def _normalize_openai_base_url(base_url: str) -> str:
    """把完整 chat completions 地址转换为 ChatOpenAI 所需的 API 根路径。"""
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized.removesuffix("/chat/completions")
    return base_url


def get_quick_think(*, observe: bool = True) -> ChatOpenAI:
    """快速模型，用于意图分类和简单任务。

    Args:
        observe: 是否纳入可观测性/计费。默认 True —— 挂载可观测性 callbacks
            （token 用量统计 + agent 追踪），主链路计费依赖此行为，存量调用
            无需改动；False —— 不挂 callbacks，用于 reasoning 旁路（问题 17，
            2026-08-11 拍板），该旁路 token 不计入用户账单。
    """
    return ChatOpenAI(
        model=settings.quick_think_model,
        api_key=SecretStr(settings.openai_api_key),
        base_url=_normalize_openai_base_url(settings.openai_base_url),
        temperature=settings.quick_think_temperature,
        # max_tokens 是 ChatOpenAI 的 Pydantic Field，mypy 无 plugin 无法识别
        max_tokens=settings.quick_think_max_tokens,  # type: ignore[call-arg]
        # 可观测性回调：token 用量统计 + agent 追踪（不侵入业务逻辑）
        # observe=False 时（reasoning 旁路）不挂，token 不计入用户账单（问题 17）
        callbacks=_get_observability_callbacks() if observe else None,
    )


def get_deep_think(
    *,
    extra_body: dict[str, Any] | None = None,
    max_tokens: int | None = None,
) -> ChatOpenAI:
    """深度模型，用于复杂分析和推理

    支持独立 API 配置（DEEP_THINK_API_KEY / DEEP_THINK_BASE_URL），
    若未配置则 fallback 到默认 OPENAI_API_KEY / OPENAI_BASE_URL。
    max_tokens：按调用覆盖全局 deep_think_max_tokens（如变体生成需要
    输出完整文件内容，4000 默认值会被截断）。
    """
    api_key = settings.deep_think_api_key or settings.openai_api_key
    base_url = settings.deep_think_base_url or settings.openai_base_url
    return ChatOpenAI(
        model=settings.deep_think_model,
        api_key=SecretStr(api_key),
        base_url=_normalize_openai_base_url(base_url),
        temperature=settings.deep_think_temperature,
        # max_tokens 是 ChatOpenAI 的 Pydantic Field，mypy 无 plugin 无法识别
        max_tokens=max_tokens if max_tokens is not None else settings.deep_think_max_tokens,  # type: ignore[call-arg]
        callbacks=_get_observability_callbacks(),
        extra_body=extra_body,
    )


def with_chat_structured_output(
    llm: ChatOpenAI,
    schema: type[BaseModel],
) -> Runnable[Any, Any]:
    """CHAT 链路专用结构化输出：固定 json_mode，避免 tool_choice。

    DeepSeek thinking mode 不支持 tool_choice（报错 "Thinking mode does not support
    this tool_choice"），而 ChatOpenAI 默认 method="function_calling" 会生成 tool_choice。
    CHAT 节点（qa_router / synth_answer）必须走 json_mode 绕开该限制。
    """
    return llm.with_structured_output(schema, method="json_mode")
