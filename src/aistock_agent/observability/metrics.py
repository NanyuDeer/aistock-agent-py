"""指标收集器 — 累计 token 用量、调用次数、错误率

线程安全（threading.Lock），供回调 handler 写入、``/metrics`` 端点读取。
不侵入业务逻辑：业务代码不感知此模块，仅 observability.callback 调用。

设计：
- ``MetricsCollector`` 实例负责累加计数器。
- 模块级单例 ``_collector`` 全局共享，回调 handler 通过
  ``get_metrics_collector()`` 获取同一实例。
- ``get_metrics()`` 返回快照字典，供端点暴露。
- ``reset()`` 供测试重置（生产一般不调用）。
"""

from __future__ import annotations

import threading


class MetricsCollector:
    """累计 LLM / 工具调用指标。

    所有写方法加锁，保证多线程并发安全。``get_metrics`` 返回快照（深拷贝数值）。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._llm_calls = 0
        self._llm_errors = 0
        self._tool_calls = 0
        self._tool_errors = 0
        self._prompt_tokens = 0
        self._completion_tokens = 0
        self._total_tokens = 0

    def record_llm_start(self, model: str = "") -> None:
        """记录一次 LLM 调用开始（调用次数 +1）。

        Args:
            model: 模型名称（当前仅用于日志，不单独累计）。
        """
        del model  # 预留：未来可按模型分桶统计
        with self._lock:
            self._llm_calls += 1

    def record_llm_tokens(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        """累计一次 LLM 调用的 token 用量。

        Args:
            prompt_tokens: 提示 token 数。
            completion_tokens: 补全 token 数。
            total_tokens: 总 token 数。
        """
        with self._lock:
            self._prompt_tokens += prompt_tokens
            self._completion_tokens += completion_tokens
            self._total_tokens += total_tokens

    def record_llm_error(self) -> None:
        """记录一次 LLM 调用错误（错误次数 +1）。"""
        with self._lock:
            self._llm_errors += 1

    def record_tool_start(self, tool_name: str = "") -> None:
        """记录一次工具调用开始（工具调用次数 +1）。

        Args:
            tool_name: 工具名称（当前仅用于日志，不单独累计）。
        """
        del tool_name  # 预留：未来可按工具分桶统计
        with self._lock:
            self._tool_calls += 1

    def record_tool_error(self) -> None:
        """记录一次工具调用错误（工具错误次数 +1）。"""
        with self._lock:
            self._tool_errors += 1

    def get_metrics(self) -> dict[str, object]:
        """返回当前累计指标快照。

        Returns:
            含 llm_calls/llm_errors/llm_error_rate/tool_calls/tool_errors/
            prompt_tokens/completion_tokens/total_tokens 的字典。
        """
        with self._lock:
            llm_calls = self._llm_calls
            llm_errors = self._llm_errors
            tool_calls = self._tool_calls
            tool_errors = self._tool_errors
            prompt_tokens = self._prompt_tokens
            completion_tokens = self._completion_tokens
            total_tokens = self._total_tokens
        error_rate = (llm_errors / llm_calls) if llm_calls > 0 else 0.0
        return {
            "llm_calls": llm_calls,
            "llm_errors": llm_errors,
            "llm_error_rate": error_rate,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    def reset(self) -> None:
        """重置所有计数器（供测试使用）。"""
        with self._lock:
            self._llm_calls = 0
            self._llm_errors = 0
            self._tool_calls = 0
            self._tool_errors = 0
            self._prompt_tokens = 0
            self._completion_tokens = 0
            self._total_tokens = 0


# 模块级单例：全局共享，回调 handler 与端点读取同一实例。
_collector = MetricsCollector()


def get_metrics_collector() -> MetricsCollector:
    """返回全局 MetricsCollector 单例。"""
    return _collector


def get_metrics() -> dict[str, object]:
    """返回当前累计指标快照（供 /metrics 端点）。"""
    return _collector.get_metrics()
