"""services/token_usage 单测 — contextvar 采集层。

覆盖：reset 后累计 snapshot / 未 reset 自动创建 / 全 0 返回 None /
callback on_llm_end 触发 record（复用 test_observability_callback.py 的
LLMResult 构造模式）。每个用例前 reset，避免 contextvar 跨用例污染。
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from langchain_core.outputs import LLMResult

from aistock_agent.observability.callback import TokenUsageCallback
from aistock_agent.observability.metrics import MetricsCollector
from aistock_agent.services import token_usage as token_usage_module
from aistock_agent.services.token_usage import (
    TokenUsageAccumulator,
    get_token_usage,
    record_token_usage,
    reset_token_usage,
)


@pytest.fixture(autouse=True)
def _reset_usage() -> None:
    """每个用例前重置 contextvar，防止跨用例污染。"""
    reset_token_usage()


def _make_llm_result(token_usage: dict[str, int] | None) -> LLMResult:
    llm_output: dict[str, object] | None = (
        None if token_usage is None else {"token_usage": token_usage}
    )
    return LLMResult(generations=[], llm_output=llm_output)


def test_reset_then_snapshot_is_none() -> None:
    """reset 后未发生任何 LLM 调用 → get_token_usage() 为 None（全 0）。"""
    assert get_token_usage() is None


def test_accumulator_add_and_snapshot() -> None:
    """累加器跨多次 record 累计，snapshot 返回三项。"""
    reset_token_usage()
    record_token_usage(10, 20, 30)
    record_token_usage(1, 2, 3)
    assert get_token_usage() == {
        "prompt_tokens": 11,
        "completion_tokens": 22,
        "total_tokens": 33,
    }


def test_record_without_reset_auto_creates() -> None:
    """未 reset 直接 record（非 chat 场景/worker 独立运行）→ 自动创建不报错。"""
    token_usage_module._usage_var.set(None)  # 模拟"从未 reset"的初始状态
    record_token_usage(5, 5, 10)
    assert get_token_usage() == {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}


def test_all_zero_returns_none() -> None:
    """全部 record 0 → snapshot 仍为 None（全 0 归一）。"""
    reset_token_usage()
    record_token_usage(0, 0, 0)
    assert get_token_usage() is None


def test_accumulator_snapshot_all_zero_is_none() -> None:
    """TokenUsageAccumulator.snapshot 全 0 → None。"""
    acc = TokenUsageAccumulator()
    assert acc.snapshot() is None
    acc.add(1, 2, 3)
    assert acc.snapshot() == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}


def test_callback_on_llm_end_triggers_record() -> None:
    """TokenUsageCallback.on_llm_end → record_token_usage（contextvar 累计）。"""
    cb = TokenUsageCallback(metrics=MetricsCollector())
    cb.on_llm_end(
        _make_llm_result({"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}),
        run_id=uuid4(),
    )
    assert get_token_usage() == {
        "prompt_tokens": 10,
        "completion_tokens": 20,
        "total_tokens": 30,
    }


def test_callback_without_token_usage_does_not_record() -> None:
    """on_llm_end 无 token_usage → 不 record，get_token_usage() 仍为 None。"""
    cb = TokenUsageCallback(metrics=MetricsCollector())
    cb.on_llm_end(_make_llm_result(None), run_id=uuid4())
    assert get_token_usage() is None
