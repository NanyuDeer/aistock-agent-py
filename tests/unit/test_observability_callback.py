"""observability.callback 单测 — TokenUsageCallback / AgentTraceCallback

验证：
- TokenUsageCallback.on_llm_start → MetricsCollector.llm_calls +1
- TokenUsageCallback.on_chat_model_start → 委托给 on_llm_start（chat 模型路径）
- TokenUsageCallback.on_llm_end → 提取 token_usage 累计到 metrics
- TokenUsageCallback.on_llm_end 无 token_usage 时不崩溃
- TokenUsageCallback.on_llm_error → llm_errors +1
- AgentTraceCallback.on_tool_start → tool_calls +1
- AgentTraceCallback.on_tool_end → 不崩溃
- AgentTraceCallback.on_tool_error → tool_errors +1
- AgentTraceCallback.on_agent_action / on_agent_finish → 不崩溃
- get_default_callbacks 返回两个 handler
- 回调使用注入的 MetricsCollector（不污染全局单例）
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.outputs import LLMResult

from aistock_agent.observability.callback import (
    AgentTraceCallback,
    TokenUsageCallback,
    get_default_callbacks,
)
from aistock_agent.observability.metrics import MetricsCollector


@pytest.fixture
def metrics() -> MetricsCollector:
    """每个测试独立的 MetricsCollector（不污染全局单例）。"""
    return MetricsCollector()


def _make_llm_result(token_usage: dict[str, int] | None) -> LLMResult:
    """构造含 token_usage 的 LLMResult。"""
    llm_output: dict[str, object] | None
    if token_usage is None:
        llm_output = None
    else:
        llm_output = {"token_usage": token_usage}
    return LLMResult(generations=[], llm_output=llm_output)


# ── TokenUsageCallback ───────────────────────────────────────────


def test_on_llm_start_increments_call_count(metrics: MetricsCollector):
    """on_llm_start → llm_calls +1"""
    cb = TokenUsageCallback(metrics=metrics)
    cb.on_llm_start({"name": "ChatOpenAI"}, [], run_id=uuid4())
    assert metrics.get_metrics()["llm_calls"] == 1


def test_on_chat_model_start_delegates_to_llm_start(metrics: MetricsCollector):
    """on_chat_model_start 委托给 on_llm_start（ChatOpenAI 走此路径）"""
    cb = TokenUsageCallback(metrics=metrics)
    cb.on_chat_model_start(
        {"name": "ChatOpenAI"}, [], run_id=uuid4(),
    )
    assert metrics.get_metrics()["llm_calls"] == 1


def test_on_llm_end_records_token_usage(metrics: MetricsCollector):
    """on_llm_end 提取 token_usage 累计到 metrics"""
    cb = TokenUsageCallback(metrics=metrics)
    result = _make_llm_result(
        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    )
    cb.on_llm_end(result, run_id=uuid4())
    m = metrics.get_metrics()
    assert m["prompt_tokens"] == 10
    assert m["completion_tokens"] == 20
    assert m["total_tokens"] == 30


def test_on_llm_end_without_token_usage_does_not_crash(metrics: MetricsCollector):
    """on_llm_end 无 token_usage 时不崩溃，不累计 token"""
    cb = TokenUsageCallback(metrics=metrics)
    result = _make_llm_result(None)
    cb.on_llm_end(result, run_id=uuid4())
    m = metrics.get_metrics()
    assert m["prompt_tokens"] == 0
    assert m["total_tokens"] == 0


def test_on_llm_end_with_partial_token_usage(metrics: MetricsCollector):
    """on_llm_end token_usage 缺少部分字段时用 0 填充"""
    cb = TokenUsageCallback(metrics=metrics)
    result = _make_llm_result({"prompt_tokens": 5})
    cb.on_llm_end(result, run_id=uuid4())
    m = metrics.get_metrics()
    assert m["prompt_tokens"] == 5
    assert m["completion_tokens"] == 0
    assert m["total_tokens"] == 0


def test_on_llm_error_increments_error_count(metrics: MetricsCollector):
    """on_llm_error → llm_errors +1"""
    cb = TokenUsageCallback(metrics=metrics)
    cb.on_llm_error(RuntimeError("timeout"), run_id=uuid4())
    assert metrics.get_metrics()["llm_errors"] == 1


def test_llm_error_rate_calculation(metrics: MetricsCollector):
    """error_rate = llm_errors / llm_calls"""
    cb = TokenUsageCallback(metrics=metrics)
    cb.on_llm_start({"name": "m"}, [], run_id=uuid4())
    cb.on_llm_start({"name": "m"}, [], run_id=uuid4())
    cb.on_llm_error(RuntimeError("err"), run_id=uuid4())
    m = metrics.get_metrics()
    assert m["llm_calls"] == 2
    assert m["llm_errors"] == 1
    assert m["llm_error_rate"] == 0.5


def test_token_usage_accumulates_across_calls(metrics: MetricsCollector):
    """多次 on_llm_end 累加 token"""
    cb = TokenUsageCallback(metrics=metrics)
    cb.on_llm_end(
        _make_llm_result(
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ),
        run_id=uuid4(),
    )
    cb.on_llm_end(
        _make_llm_result(
            {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        ),
        run_id=uuid4(),
    )
    m = metrics.get_metrics()
    assert m["prompt_tokens"] == 30
    assert m["completion_tokens"] == 15
    assert m["total_tokens"] == 45


# ── AgentTraceCallback ───────────────────────────────────────────


def test_on_tool_start_increments_tool_calls(metrics: MetricsCollector):
    """on_tool_start → tool_calls +1"""
    cb = AgentTraceCallback(metrics=metrics)
    cb.on_tool_start({"name": "get_quote"}, "000001", run_id=uuid4())
    assert metrics.get_metrics()["tool_calls"] == 1


def test_on_tool_end_does_not_crash(metrics: MetricsCollector):
    """on_tool_end 不崩溃"""
    cb = AgentTraceCallback(metrics=metrics)
    cb.on_tool_end("some output", run_id=uuid4())
    assert metrics.get_metrics()["tool_calls"] == 0


def test_on_tool_error_increments_tool_errors(metrics: MetricsCollector):
    """on_tool_error → tool_errors +1"""
    cb = AgentTraceCallback(metrics=metrics)
    cb.on_tool_error(ValueError("bad input"), run_id=uuid4())
    assert metrics.get_metrics()["tool_errors"] == 1


def test_on_agent_action_does_not_crash(metrics: MetricsCollector):
    """on_agent_action 不崩溃"""
    cb = AgentTraceCallback(metrics=metrics)
    action = AgentAction(tool="get_quote", tool_input={"symbol": "000001"}, log="")
    cb.on_agent_action(action, run_id=uuid4())


def test_on_agent_finish_does_not_crash(metrics: MetricsCollector):
    """on_agent_finish 不崩溃"""
    cb = AgentTraceCallback(metrics=metrics)
    finish = AgentFinish(return_values={"output": "done"}, log="")
    cb.on_agent_finish(finish, run_id=uuid4())


# ── get_default_callbacks ────────────────────────────────────────


def test_get_default_callbacks_returns_both_handlers():
    """get_default_callbacks 返回 TokenUsageCallback + AgentTraceCallback"""
    callbacks = get_default_callbacks()
    assert len(callbacks) == 2
    assert any(isinstance(c, TokenUsageCallback) for c in callbacks)
    assert any(isinstance(c, AgentTraceCallback) for c in callbacks)


def test_get_default_callbacks_share_global_metrics():
    """get_default_callbacks 的 handler 共享全局 MetricsCollector 单例"""
    from aistock_agent.observability.metrics import get_metrics_collector

    callbacks = get_default_callbacks()
    global_metrics = get_metrics_collector()
    for cb in callbacks:
        assert cb._metrics is global_metrics  # noqa: SLF001


def test_metrics_reset_clears_all_counters(metrics: MetricsCollector):
    """reset 后所有计数器归零"""
    cb = TokenUsageCallback(metrics=metrics)
    cb.on_llm_start({"name": "m"}, [], run_id=uuid4())
    cb.on_llm_end(
        _make_llm_result(
            {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        ),
        run_id=uuid4(),
    )
    assert metrics.get_metrics()["llm_calls"] == 1
    metrics.reset()
    m = metrics.get_metrics()
    assert m["llm_calls"] == 0
    assert m["prompt_tokens"] == 0
