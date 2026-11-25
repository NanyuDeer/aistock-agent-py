"""节点级 RetryPolicy 行为测试（2026-11-18，框架升级项 2）。

覆盖三件事：
1. 传输层异常（TimeoutError）会重跑节点并最终成功；
2. 业务异常（ValueError）**不重试**，一次即抛（不能被当成"网络抖动"重跑）；
3. ``io_retry_policy()`` 的参数与 ``retry_on`` 语义锁定（防止后续被误改宽）。
"""
from __future__ import annotations

from typing import Any, TypedDict

import openai
import pytest
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from aistock_agent.graph.retry import io_retry_policy, is_transient_error


class _S(TypedDict, total=False):
    out: str


def _graph_with(node: Any, policy: RetryPolicy):  # noqa: ANN202 - 测试辅助
    g: StateGraph = StateGraph(_S)
    g.add_node("n", node, retry_policy=policy)
    g.add_edge(START, "n")
    g.add_edge("n", END)
    return g.compile()


def _fast_policy() -> RetryPolicy:
    """与 io_retry_policy 同语义但零退避，避免测试真的等 3 秒。"""
    return RetryPolicy(
        max_attempts=3,
        initial_interval=0.0,
        backoff_factor=1.0,
        max_interval=0.0,
        jitter=False,
        retry_on=is_transient_error,
    )


@pytest.mark.asyncio
async def test_transient_error_is_retried_until_success() -> None:
    calls = {"n": 0}

    async def flaky(_state: _S) -> dict[str, Any]:
        calls["n"] += 1
        if calls["n"] < 3:
            raise TimeoutError("upstream read timeout")
        return {"out": "ok"}

    result = await _graph_with(flaky, _fast_policy()).ainvoke({})

    assert calls["n"] == 3
    assert result["out"] == "ok"


@pytest.mark.asyncio
async def test_business_error_is_not_retried() -> None:
    calls = {"n": 0}

    async def broken(_state: _S) -> dict[str, Any]:
        calls["n"] += 1
        raise ValueError("business rule violated")

    with pytest.raises(ValueError):
        await _graph_with(broken, _fast_policy()).ainvoke({})

    assert calls["n"] == 1, "业务异常不得被当成传输抖动重跑"


@pytest.mark.asyncio
async def test_retry_gives_up_after_max_attempts() -> None:
    calls = {"n": 0}

    async def always_timeout(_state: _S) -> dict[str, Any]:
        calls["n"] += 1
        raise TimeoutError("still down")

    with pytest.raises(TimeoutError):
        await _graph_with(always_timeout, _fast_policy()).ainvoke({})

    assert calls["n"] == 3, "最多 3 次后放弃，不无限重试"


def test_io_retry_policy_parameters_are_pinned() -> None:
    policy = io_retry_policy()

    assert policy.max_attempts == 3
    assert policy.retry_on is is_transient_error


def test_transient_classification() -> None:
    assert is_transient_error(TimeoutError())
    assert is_transient_error(ConnectionError())
    assert is_transient_error(openai.APITimeoutError(request=None))  # type: ignore[arg-type]
    # 业务异常与限流均不重试：限流由 openai SDK 自身按 Retry-After 处理，
    # 节点级重试会把整个节点（含取数）重跑，代价大于收益。
    assert not is_transient_error(ValueError())
    assert not is_transient_error(RuntimeError())
