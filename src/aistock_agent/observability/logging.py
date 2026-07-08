"""structlog 结构化日志配置

集中配置 structlog，输出 JSON 格式（timestamp / level / event / request_id）。
业务代码通过 ``get_logger(name)`` 获取 BoundLogger，不直接 import structlog，
便于将来替换日志后端，且保证可观测性不侵入业务逻辑。

处理器链：
    merge_contextvars  → 合并 contextvars（request_id 等请求级上下文）
    add_log_level      → 注入 level 字段
    StackInfoRenderer  → 堆栈信息
    set_exc_info       → 异常信息
    TimeStamper(iso)   → 注入 timestamp 字段
    JSONRenderer       → 序列化为 JSON
"""

from __future__ import annotations

import logging
from typing import cast

import structlog


def setup_logging(level: str = "INFO") -> None:
    """配置 structlog 全局日志（JSON 输出）。

    在应用启动时调用一次（main.lifespan）。多次调用安全：以最后一次配置为准。

    Args:
        level: 日志级别字符串（DEBUG/INFO/WARNING/ERROR），大小写不敏感。
            无效值回退到 INFO。
    """
    log_level = _parse_level(level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )


def get_logger(name: str) -> structlog.BoundLogger:
    """获取具名 BoundLogger。

    业务代码统一通过此函数获取 logger，便于将来替换日志后端。

    Args:
        name: logger 名称，通常传 ``__name__``。

    Returns:
        structlog BoundLogger 实例（已绑定 name）。
    """
    # structlog.get_logger 返回 Any，此处 cast 为 BoundLogger 以满足类型约束。
    return cast(structlog.BoundLogger, structlog.get_logger(name))


def _parse_level(level: str) -> int:
    """将日志级别字符串解析为 logging 模块整数级别。

    Args:
        level: 级别字符串，大小写不敏感。

    Returns:
        logging 级别整数；无效值回退 logging.INFO。
    """
    raw = getattr(logging, level.upper(), logging.INFO)
    if not isinstance(raw, int):
        return logging.INFO
    return raw
