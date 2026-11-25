"""节点级重试策略 — 只管传输层抖动，不管语义纠错。

为什么单独成模块
----------------
仓库里有两类"重试"，职责不同，**不能互相替代**：

1. **传输层重试**（本模块）：节点因网络抖动/超时/连接断开抛异常 → 重跑该节点。
   无状态、可重入、不改变业务语义。
2. **语义纠错重试**（``agents/workers/review.py:_generate_trace_with_retry``、
   ``agents/workers/stock_trace.py`` 的 ``for attempt in range(2)``）：
   LLM 输出不合规、确定性校验失败 → 把错误详情回灌给模型再试一次。
   这类重试带反馈、有 token 成本、不可由 RetryPolicy 表达，必须保留原实现。

覆盖面为什么只有三个节点
------------------------
``skills/base.py`` 的 ``@skill`` 装饰器与各 worker 的顶层 ``try/except``
都会把异常转成 degraded 结果/降级文本 —— 那些节点**不会抛异常**，挂
RetryPolicy 等于挂了个永不触发的装饰。因此只给真正会把异常抛到图上的节点挂：

- ``general_fallback``（chat 图）与 ``general_agent`` / ``alert_agent``
  （supervisor 图）：内部是 ``create_react_agent``，LLM 传输异常会原样抛出。

明确不挂的节点及原因：
- ``synth_answer`` / ``qa_router`` / 各闸门节点：重试会重复调用 LLM →
  重复计费、重复流式输出、重复分类。
- ``escalate`` / supervisor 的取数 worker：内部已降级，重试只会让耗时翻倍。
"""

from __future__ import annotations

import httpx
import openai
from langgraph.types import RetryPolicy

# 只认"网络层抖动"这一类。刻意不含 RateLimitError / 5xx：
# - 限流由 openai SDK 自身按 Retry-After 处理，节点级重试会把整个 worker
#   连同它的取数一起重跑，代价远大于收益；
# - 5xx 同理，且重跑不确定是否幂等。
# asyncio.TimeoutError 在 3.11 已是 TimeoutError 的别名，无需重复列出。
_TRANSIENT_ERRORS: tuple[type[Exception], ...] = (
    TimeoutError,
    ConnectionError,
    httpx.TransportError,  # 含 ConnectError / ReadTimeout / RemoteProtocolError
    openai.APITimeoutError,
    openai.APIConnectionError,
)


def is_transient_error(exc: Exception) -> bool:
    """判断异常是否属于可安全重跑的传输层错误。

    供 ``RetryPolicy(retry_on=...)`` 使用；也可在业务侧复用做降级判定。
    ``asyncio.CancelledError`` 继承 BaseException（3.8+），不会进入本判断，
    客户端断开/任务取消不会被重试。
    """
    return isinstance(exc, _TRANSIENT_ERRORS)


def io_retry_policy() -> RetryPolicy:
    """取数/LLM 传输层重试：最多 3 次，1s 起指数退避，上限 8s。

    用函数而非模块级常量，避免调用方误改共享实例（RetryPolicy 非 frozen）。
    """
    return RetryPolicy(
        max_attempts=3,
        initial_interval=1.0,
        backoff_factor=2.0,
        max_interval=8.0,
        jitter=True,
        retry_on=is_transient_error,
    )
