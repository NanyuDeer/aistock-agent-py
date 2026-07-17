"""tools/base.py — 通用工具基类与 safe_tool_call 装饰器

统一 ``@tool`` 函数的异常处理：捕获 → structlog 记录 → 返回降级文本，
避免单个工具异常中断整个 graph 执行（落实 A11）。

设计要点：
- 现有 4 个 tool 文件均为 ``@tool`` 函数式（langchain），故以 ``safe_tool_call``
  装饰器为主；``BaseToolMixin`` 作为可选的类式基类供未来类式工具复用。
- ``safe_tool_call`` 必须放在 ``@tool`` **下方**：先包装原函数（保留 docstring/
  signature），再由 ``@tool`` 据内省结果生成 schema。顺序反了会让 langchain 拿到
  装饰器 wrapper 的签名而非业务签名。
"""

import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec

import structlog

logger = structlog.get_logger()

P = ParamSpec("P")

#: 工具异常降级文本（稳定不变，前端/测试可据此断言）
DEGRADED_MESSAGE = "实时连接受限，请结合你的公开知识继续分析（行业地位、近期走势、市场情绪），标注'模拟分析'"


def safe_tool_call(func: Callable[P, Awaitable[str]]) -> Callable[P, Awaitable[str]]:
    """装饰器：为 async @tool 函数统一包装 try-catch + 错误日志 + 降级返回。

    用法（必须放在 ``@tool`` 下方）::

        @tool
        @safe_tool_call
        async def get_quote(symbol: str) -> str:
            \"\"\"查询行情\"\"\"
            ...

    行为契约：
    - 正常：透传原函数返回值（含 falsy 值，不吞掉空串等合法返回）。
    - 异常：捕获 ``Exception``，structlog error 记录工具名+错误+堆栈，
      返回 :data:`DEGRADED_MESSAGE`，使 graph 收到降级文本而非崩溃。
    - 元数据：``functools.wraps`` 保留 ``__name__``/``__doc__``/signature，
      确保 langchain ``@tool`` 能据 docstring 生成正确的 tool schema。

    仅支持 async 函数（现有 tool 文件均为 async）。同步工具请先转 async。
    """

    @functools.wraps(func)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> str:
        try:
            return await func(*args, **kwargs)
        except Exception as e:
            logger.error(
                "tool_call_failed",
                tool=func.__name__,
                error=str(e),
                exc_info=True,
            )
            return DEGRADED_MESSAGE

    return wrapper


class BaseToolMixin:
    """类式工具的可选基类（供未来类式工具复用统一降级逻辑）。

    现有 4 个 tool 文件均为 ``@tool`` 函数式，直接使用 :func:`safe_tool_call`
    装饰器即可。本 Mixin 预留类式工具入口，Task 9+ 若引入类式工具可继承并复用
    :meth:`safe_call`，避免重复实现 try-catch/日志/降级逻辑。
    """

    @staticmethod
    async def safe_call(coro: Awaitable[str], *, tool_name: str) -> str:
        """对已有 coroutine 统一捕获异常并降级（类式工具的等价入口）。

        与 :func:`safe_tool_call` 共用同一套降级文本与日志格式。
        """
        try:
            return await coro
        except Exception as e:
            logger.error(
                "tool_call_failed",
                tool=tool_name,
                error=str(e),
                exc_info=True,
            )
            return DEGRADED_MESSAGE
