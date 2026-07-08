"""可观测性模块 — 结构化日志、回调追踪、指标统计

对外接口：
- ``logging.setup_logging`` / ``logging.get_logger`` — structlog JSON 日志
- ``callback.TokenUsageCallback`` / ``AgentTraceCallback`` / ``get_default_callbacks``
  — LangChain 回调（token 用量 + agent 追踪）
- ``metrics.MetricsCollector`` / ``get_metrics`` — 累计指标

设计原则：可观测性不得侵入业务逻辑，仅通过 callback / middleware 解耦。
"""

from aistock_agent.observability.callback import (
    AgentTraceCallback,
    TokenUsageCallback,
    get_default_callbacks,
)
from aistock_agent.observability.logging import get_logger, setup_logging
from aistock_agent.observability.metrics import MetricsCollector, get_metrics

__all__ = [
    "setup_logging",
    "get_logger",
    "TokenUsageCallback",
    "AgentTraceCallback",
    "get_default_callbacks",
    "MetricsCollector",
    "get_metrics",
]
