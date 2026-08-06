"""LangGraph checkpointer 工厂 — 根据 config 选择后端，单例缓存

开发默认 MemorySaver（无新依赖，已可用）。sqlite/redis 后端需安装对应
``langgraph-checkpoint-sqlite`` / ``langgraph-checkpoint-redis`` 子包；
未安装时优雅降级到 MemorySaver 并通过 structlog 发出 warning。

env 声明 ``CHECKPOINTER_BACKEND`` 选择后端；实际后端取决于已安装的包：
- memory：MemorySaver（默认，零依赖）
- sqlite：AsyncSqliteSaver（.langgraph.db 文件，跨进程/重启持久；async 后端，
  与 chat 子图的 astream_events/ainvoke 异步执行兼容）
- redis：RedisSaver（需 Redis 实例支持 RedisJSON 模块，见 task-7 报告）
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import threading
from collections.abc import Coroutine
from contextlib import AbstractContextManager
from typing import Any, TypeVar

import structlog
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import MemorySaver

from aistock_agent.config import settings

logger = structlog.get_logger()


def _ensure_aiosqlite_compat() -> None:
    """补 aiosqlite.Connection.is_alive（langgraph-checkpoint-sqlite 2.0.11 需要）。

    AsyncSqliteSaver.setup() 会调用 ``self.conn.is_alive()``，但该 API 截至
    aiosqlite 0.22.1（PyPI 最新版）尚未发布，2.0.11 是 langgraph-checkpoint
    2.1.2（langgraph 0.2.74 锁定）兼容的最新 sqlite 子包，无上游修复可用。
    补等价实现：aiosqlite 连接延迟建立，底层 sqlite3 连接已建立即视为 alive。
    幂等：目标已存在则不重复打补丁。
    """
    if hasattr(aiosqlite.Connection, "is_alive"):
        return

    def is_alive(self: aiosqlite.Connection) -> bool:
        return self._connection is not None

    aiosqlite.Connection.is_alive = is_alive  # type: ignore[attr-defined]

# 单例缓存：首次调用初始化，后续返回同一实例（避免重复建连接 / 丢状态）
# V=str 与 MemorySaver(BaseCheckpointSaver[str]) 一致；sqlite/redis 未安装时
# 实际赋值来源为 Any（import 被 ignore），赋给声明类型不报错
_checkpointer: BaseCheckpointSaver[str] | None = None
# from_conn_string 返回的 context manager 必须保持存活：若临时 CM 被 GC，会向
# 挂起的生成器抛 GeneratorExit，导致 closing(sqlite3.connect)/redis 客户端被
# close（sqlite 连接不可重开，redis 客户端被关闭需重连）。随单例一起存活，
# 由 app 进程生命周期保证会话写入时底层连接仍可用。
# 注：sqlite 分支不使用 CM（见 _build_async_sqlite_saver 注释），本持有者仅
# 服务 redis 分支的同步 CM。
_checkpointer_cm: AbstractContextManager[BaseCheckpointSaver[str]] | None = None
# sqlite 分支构造成功后注册退出钩子关闭 aiosqlite 连接；幂等：进程内只注册一次
_sqlite_conn_atexit_registered = False

_T = TypeVar("_T")


def _run_coro_sync(coro: Coroutine[Any, Any, _T]) -> _T:
    """在同步代码中执行一个 coroutine（调用方可能身处 async 上下文）。

    get_checkpointer() 是同步接口，但可能在已有运行中 event loop 的调用方
    （routes.py 的 async handler、async 测试）里被触发：
    - 无运行中 loop：``asyncio.run`` 直接执行；
    - 有运行中 loop：不能在当前线程阻塞等待，换独立线程跑一个临时 loop 执行。
      aiosqlite 连接自带独立工作线程（``future.get_loop().call_soon_threadsafe``
      回投结果），不依赖创建时的 loop，后续在服务 loop 中 await 照常工作。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future: concurrent.futures.Future[_T] = pool.submit(asyncio.run, coro)
        return future.result()


def _close_sqlite_conn_on_exit() -> None:
    """退出钩子：关闭 sqlite 后端持有的 aiosqlite 连接。

    aiosqlite 连接的工作线程是非 daemon 的（core.py 中 Thread() 未设
    daemon=True），若不在解释器退出前显式 close，Python 会等待该线程结束 →
    优雅退出（uvicorn / pm2 stop）永久挂起。close() 是 async 方法，复用
    _run_coro_sync 执行（无运行中 loop 走 asyncio.run，有则换独立线程）；
    异常一律吞掉，保证退出阶段永不抛错。已关闭的连接再次 close 是幂等空操作
    （aiosqlite.Connection.close 对 _connection is None 直接返回）。
    """
    conn = getattr(_checkpointer, "conn", None)
    close = getattr(conn, "close", None)
    if close is None:
        return
    try:
        _run_coro_sync(close())
    except Exception:
        logger.warning("failed to close sqlite checkpointer connection on exit")


async def _build_async_sqlite_saver() -> BaseCheckpointSaver[str]:
    """构造 AsyncSqliteSaver（aiosqlite 连接直建，不经 async CM）。

    不用 ``AsyncSqliteSaver.from_conn_string`` 的 async CM：它是 async context
    manager，只能 await __aenter__；而同步进入后该挂起的 async generator 无法
    保证存活——``asyncio.run`` 退出时会 ``shutdown_asyncgens()`` 关闭所有挂起的
    async generator，连接随之被关（实测 aiosqlite ``threads can only be started
    once``）。改为 aio.py 文档中的 Raw usage 模式：``aiosqlite.connect`` 直建
    连接 + ``AsyncSqliteSaver(conn)``。连接由 ``_checkpointer`` 单例持有，随 app
    进程生命周期存活，与 _checkpointer_cm 的 GC 防护目的等价（无 CM 即无
    GeneratorExit 关连接风险）。
    """
    # 懒加载 aiosqlite：它是 sqlite 后端专属依赖，若在模块顶层 import，缺包时
    # 连 memory 后端（docstring 声明"零依赖"）都无法启动，且 get_checkpointer()
    # 的 try/except ImportError 降级永远不会执行。改在此处导入，ImportError 会
    # 沿 _run_coro_sync 冒到 sqlite 分支的 except → 降级 MemorySaver。
    # global：使绑定落在模块命名空间，供 _ensure_aiosqlite_compat() 引用。
    global aiosqlite
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    conn = await aiosqlite.connect(settings.sqlite_path)
    return AsyncSqliteSaver(conn)


def get_checkpointer() -> BaseCheckpointSaver[str]:
    """返回 checkpointer 单例。

    根据 ``settings.checkpointer_backend`` 选择后端：
    - ``"memory"``：MemorySaver（开发默认，已可用）
    - ``"sqlite"``：AsyncSqliteSaver（需 langgraph-checkpoint-sqlite + aiosqlite），
      import 失败降级 MemorySaver
    - ``"redis"``：RedisSaver（需 langgraph-checkpoint-redis），import 失败降级 MemorySaver

    单例缓存：首次调用初始化，后续返回同一实例。
    """
    global _checkpointer, _checkpointer_cm, _sqlite_conn_atexit_registered
    if _checkpointer is not None:
        return _checkpointer

    backend = settings.checkpointer_backend
    if backend == "sqlite":
        try:
            # AsyncSqliteSaver：chat 子图走 astream_events/ainvoke 异步执行，
            # 同步 SqliteSaver 的 async 方法会抛 NotImplementedError（生产阻塞，
            # 见 review 结论）。连接构造见 _build_async_sqlite_saver（直建连接、
            # 不经 async CM，CM 无法在同步入口下存活）。
            _checkpointer = _run_coro_sync(_build_async_sqlite_saver())
            # aiosqlite 已在 _build_async_sqlite_saver 内懒加载成功，此处才可
            # 引用；补丁必须在首次持久化（setup() 调 is_alive）前应用。
            _ensure_aiosqlite_compat()
            # 注册退出时关闭连接（幂等，只注册一次）：aiosqlite 工作线程非
            # daemon，不关闭则解释器退出永久挂起（见 _close_sqlite_conn_on_exit）。
            # 不能用 atexit.register：CPython 3.11 实测 atexit 回调在
            # threading._shutdown join 非 daemon 线程【之后】才运行，届时进程
            # 已挂死。改用 threading._register_atexit（stdlib concurrent.futures
            # 同款机制）：回调在 join 之前执行，把 close 任务投进 aiosqlite 工作
            # 队列 → 线程处理 _STOP 哨兵后退出；该 API 3.9+ 存在，缺失回退
            # atexit.register（<3.9，行为退化为挂起，项目目标 3.11 不涉及）。
            if not _sqlite_conn_atexit_registered:
                register_exit_hook = getattr(threading, "_register_atexit", atexit.register)
                register_exit_hook(_close_sqlite_conn_on_exit)
                _sqlite_conn_atexit_registered = True
            logger.info(
                "checkpointer_initialized", backend="sqlite", path=settings.sqlite_path
            )
        except ImportError:
            logger.warning("sqlite backend not available, falling back to memory")
            _checkpointer = MemorySaver()
    elif backend == "redis":
        try:
            from langgraph.checkpoint.redis import RedisSaver

            _checkpointer_cm = RedisSaver.from_conn_string(settings.redis_url)
            _checkpointer = _checkpointer_cm.__enter__()
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
