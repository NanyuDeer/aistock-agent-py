"""LangGraph checkpointer 工厂 — 根据 config 选择后端，单例缓存

开发默认 MemorySaver（无新依赖，已可用）。sqlite/redis 后端需安装对应
``langgraph-checkpoint-sqlite`` / ``langgraph-checkpoint-redis`` 子包；
未安装时优雅降级到 MemorySaver 并通过 structlog 发出 warning。

决策依据：venv 的 pip 不可用，无法安装 sqlite/redis 子包，故以 MemorySaver
作为开发默认；env 仍可声明 backend 值，实际后端取决于已安装的包。
"""

from __future__ import annotations

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from aistock_agent.config import settings

logger = structlog.get_logger()

# 单例缓存：首次调用初始化，后续返回同一实例（避免重复建连接 / 丢状态）
# V=str 与 MemorySaver(BaseCheckpointSaver[str]) 一致；sqlite/redis 未安装时
# 实际赋值来源为 Any（import 被 ignore），赋给声明类型不报错
_checkpointer: BaseCheckpointSaver[str] | None = None


def get_checkpointer() -> BaseCheckpointSaver[str]:
    """返回 checkpointer 单例。

    根据 ``settings.checkpointer_backend`` 选择后端：
    - ``"memory"``：MemorySaver（开发默认，已可用）
    - ``"sqlite"``：SqliteSaver（需 langgraph-checkpoint-sqlite），import 失败降级 MemorySaver
    - ``"redis"``：RedisSaver（需 langgraph-checkpoint-redis），import 失败降级 MemorySaver

    单例缓存：首次调用初始化，后续返回同一实例。
    """
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer

    backend = settings.checkpointer_backend
    if backend == "sqlite":
        try:
            # SqliteSaver.from_conn_string 返回 AbstractContextManager[SqliteSaver]，
            # 进入上下文获取 saver 实例（单例随应用生命周期存活）
            from langgraph.checkpoint.sqlite import SqliteSaver  # type: ignore[import-not-found]

            _checkpointer = SqliteSaver.from_conn_string(
                settings.sqlite_path
            ).__enter__()
            logger.info(
                "checkpointer_initialized", backend="sqlite", path=settings.sqlite_path
            )
        except ImportError:
            logger.warning("sqlite backend not available, falling back to memory")
            _checkpointer = MemorySaver()
    elif backend == "redis":
        try:
            from langgraph.checkpoint.redis import RedisSaver  # type: ignore[import-not-found]

            _checkpointer = RedisSaver.from_conn_string(
                settings.redis_url
            ).__enter__()
            logger.info(
                "checkpointer_initialized", backend="redis", url=settings.redis_url
            )
        except ImportError:
            logger.warning("redis backend not available, falling back to memory")
            _checkpointer = MemorySaver()
    else:
        _checkpointer = MemorySaver()
        logger.info("checkpointer_initialized", backend="memory")

    return _checkpointer
