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
from httpx import ReadTimeout
from langchain_core.agents import AgentAction, AgentFinish
from langchain_core.outputs import LLMResult

from aistock_agent.observability.callback import (
    AgentTraceCallback,
    LatencyCallback,
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


# ── LatencyCallback（请求级耗时诊断埋点）────────────────────────


def _recorder():
    """返回一个收集 (event, kwargs) 的列表记录器。"""
    records: list[dict] = []

    def sink(**kwargs):
        records.append(kwargs)

    return records, sink


def test_latency_callback_records_total_ms_on_end():
    """on_chat_model_start → on_llm_end 记录耗时，ms >= 0 且带 run_id。"""
    records, sink = _recorder()
    cb = LatencyCallback(log_sink=sink)
    rid = uuid4()
    cb.on_chat_model_start({"name": "ChatOpenAI"}, [], run_id=rid)
    cb.on_llm_end(_make_llm_result(None), run_id=rid)
    assert len(records) == 1
    assert records[0]["event"] == "llm.call.duration"
    assert records[0]["run_id"] == str(rid)
    assert isinstance(records[0]["total_ms"], float)
    assert records[0]["total_ms"] >= 0


def test_latency_callback_records_first_token_ms_on_stream():
    """前串流第一个 token 记录首 token 延迟；end 记录总时长。"""
    records, sink = _recorder()
    cb = LatencyCallback(log_sink=sink)
    rid = uuid4()
    cb.on_chat_model_start({"name": "ChatOpenAI"}, [], run_id=rid)
    cb.on_llm_new_token("部分", run_id=rid)
    cb.on_llm_end(_make_llm_result(None), run_id=rid)
    types = [r["event"] for r in records]
    assert "llm.call.duration" in types
    first_token = next(
        (r for r in records if r["event"] == "llm.call.first_token"), None
    )
    assert first_token is not None
    assert "tokens_ms" in first_token


def test_latency_callback_records_error_type():
    """on_llm_error 记录错误时总耗时与错误类型。"""
    records, sink = _recorder()
    cb = LatencyCallback(log_sink=sink)
    rid = uuid4()
    cb.on_chat_model_start({"name": "ChatOpenAI"}, [], run_id=rid)
    err = ReadTimeout("read timed out")
    cb.on_llm_error(err, run_id=rid)
    assert len(records) == 1
    assert records[0]["event"] == "llm.call.error"
    assert records[0]["error_type"] == "ReadTimeout"
    assert records[0]["total_ms"] >= 0


def test_latency_callback_missing_start_does_not_crash():
    """异常路径下 end/error 无对应 start 时不崩溃、不产生记录。"""
    records, sink = _recorder()
    cb = LatencyCallback(log_sink=sink)
    cb.on_llm_end(_make_llm_result(None), run_id=uuid4())
    cb.on_llm_error(RuntimeError("x"), run_id=uuid4())
    assert records == []


def test_get_default_callbacks_includes_latency():
    """get_default_callbacks 含 LatencyCallback（诊断埋点默认开启）。"""
    callbacks = get_default_callbacks()
    assert any(isinstance(c, LatencyCallback) for c in callbacks)


def test_get_default_callbacks_returns_three_handlers():
    """get_default_callbacks 现返回 TokenUsage + AgentTrace + Latency。"""
    callbacks = get_default_callbacks()
    assert len(callbacks) == 3
    assert any(isinstance(c, TokenUsageCallback) for c in callbacks)
    assert any(isinstance(c, AgentTraceCallback) for c in callbacks)


# ── get_default_callbacks ────────────────────────────────────────


def test_get_default_callbacks_returns_both_handlers():
    """get_default_callbacks 返回 TokenUsageCallback + AgentTraceCallback。"""
    callbacks = get_default_callbacks()
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


# ── LLM 前缀缓存命中观测（2026-08-25 design-debate 产出）────────


def _make_result_with_cache(llm_output: dict[str, object]) -> LLMResult:
    """构造含缓存字段的 token_usage LLMResult（ainvoke 路径）。"""
    return LLMResult(generations=[], llm_output=llm_output)


def test_on_llm_end_records_openai_cached_tokens(metrics: MetricsCollector):
    """OpenAI prompt_tokens_details.cached_tokens → llm_cache.openai"""
    cb = TokenUsageCallback(metrics=metrics)
    result = _make_result_with_cache(
        {
            "token_usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 80},
            }
        }
    )
    cb.on_llm_end(result, run_id=uuid4())
    cache = metrics.get_metrics()["llm_cache"]["openai"]
    assert cache["prompt_tokens"] == 100
    assert cache["cached_tokens"] == 80


def test_on_llm_end_records_deepseek_cached_tokens(metrics: MetricsCollector):
    """DeepSeek prompt_cache_hit_tokens → llm_cache.deepseek"""
    cb = TokenUsageCallback(metrics=metrics)
    result = _make_result_with_cache(
        {
            "token_usage": {
                "prompt_tokens": 200,
                "completion_tokens": 20,
                "total_tokens": 220,
                "prompt_cache_hit_tokens": 150,
                "prompt_cache_miss_tokens": 50,
            }
        }
    )
    cb.on_llm_end(result, run_id=uuid4())
    cache = metrics.get_metrics()["llm_cache"]["deepseek"]
    assert cache["prompt_tokens"] == 200
    assert cache["cached_tokens"] == 150


def test_on_llm_end_no_cache_fields_ignored(metrics: MetricsCollector):
    """无缓存字段时 llm_cache 不产生条目、不崩溃；原计费路径不受影响"""
    cb = TokenUsageCallback(metrics=metrics)
    cb.on_llm_end(
        _make_llm_result(
            {"prompt_tokens": 5, "completion_tokens": 1, "total_tokens": 6},
        ),
        run_id=uuid4(),
    )
    m = metrics.get_metrics()
    assert m["llm_cache"] == {}
    assert m["prompt_tokens"] == 5
    assert m["total_tokens"] == 6


def test_llm_cache_accumulates_across_calls(metrics: MetricsCollector):
    """多次命中调用按 provider 分桶累加，hit_rate = cached / prompt"""
    cb = TokenUsageCallback(metrics=metrics)
    for cached in (10, 20):
        cb.on_llm_end(
            _make_result_with_cache(
                {
                    "token_usage": {
                        "prompt_tokens": 50,
                        "completion_tokens": 5,
                        "total_tokens": 55,
                        "prompt_tokens_details": {"cached_tokens": cached},
                    }
                }
            ),
            run_id=uuid4(),
        )
    cache = metrics.get_metrics()["llm_cache"]["openai"]
    assert cache["prompt_tokens"] == 100
    assert cache["cached_tokens"] == 30
    assert cache["hit_rate"] == 0.3
