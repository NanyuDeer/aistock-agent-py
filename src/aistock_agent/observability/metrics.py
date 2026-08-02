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
        # CHAT QA 链路指标
        self._chat_qa_latency: dict[str, dict[str, int]] = {
            "qa_router": {"sum": 0, "count": 0},
            "synth_answer": {"sum": 0, "count": 0},
            "e2e": {"sum": 0, "count": 0},
        }
        self._skill_latency: dict[str, dict[str, int]] = {}
        self._skill_degraded: dict[str, int] = {}
        self._synth_degraded = 0
        # T6：deep 升级率基础计数（按 worker 名分桶，D3）
        self._chat_qa_escalations: dict[str, int] = {}

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

    # ---- CHAT QA 链路指标 ----

    def record_chat_qa_latency(self, node: str, ms: int) -> None:
        """记录 CHAT QA 节点延迟（qa_router / synth_answer / e2e）。

        Args:
            node: 节点名，可选 "qa_router" / "synth_answer" / "e2e"。
            ms: 延迟毫秒数。
        """
        with self._lock:
            if node not in self._chat_qa_latency:
                return
            self._chat_qa_latency[node]["sum"] += ms
            self._chat_qa_latency[node]["count"] += 1

    def record_skill_latency(self, skill_name: str, ms: int) -> None:
        """记录单个 Skill 的延迟（按 skill_name 分桶）。

        Args:
            skill_name: Skill 名称。
            ms: 延迟毫秒数。
        """
        with self._lock:
            if skill_name not in self._skill_latency:
                self._skill_latency[skill_name] = {"sum": 0, "count": 0}
            self._skill_latency[skill_name]["sum"] += ms
            self._skill_latency[skill_name]["count"] += 1

    def record_skill_degraded(self, skill_name: str) -> None:
        """记录单个 Skill 降级一次（按 skill_name 分桶）。

        Args:
            skill_name: Skill 名称。
        """
        with self._lock:
            self._skill_degraded[skill_name] = self._skill_degraded.get(skill_name, 0) + 1

    def record_synth_degraded(self) -> None:
        """记录 synth_answer 降级一次。"""
        with self._lock:
            self._synth_degraded += 1

    def record_chat_qa_escalation(self, worker: str) -> None:
        """记录一次 deep 升级（按 worker 名分桶，供升级率 deep/total 计算）。

        Args:
            worker: worker 名（"stock" / "sector" / "hot_burst"）。
        """
        with self._lock:
            self._chat_qa_escalations[worker] = (
                self._chat_qa_escalations.get(worker, 0) + 1
            )

    def get_metrics(self) -> dict[str, object]:
        """返回当前累计指标快照。

        Returns:
            含 llm_calls/llm_errors/llm_error_rate/tool_calls/tool_errors/
            prompt_tokens/completion_tokens/total_tokens 的字典，
            外加 chat_qa 嵌套字段（CHAT QA 链路延迟与降级指标）。
        """
        with self._lock:
            llm_calls = self._llm_calls
            llm_errors = self._llm_errors
            tool_calls = self._tool_calls
            tool_errors = self._tool_errors
            prompt_tokens = self._prompt_tokens
            completion_tokens = self._completion_tokens
            total_tokens = self._total_tokens
            chat_qa_latency = {
                node: {"sum": v["sum"], "count": v["count"]}
                for node, v in self._chat_qa_latency.items()
            }
            skill_latency = {
                name: {"sum": v["sum"], "count": v["count"]}
                for name, v in self._skill_latency.items()
            }
            skill_degraded = dict(self._skill_degraded)
            synth_degraded = self._synth_degraded
            chat_qa_escalations = dict(self._chat_qa_escalations)
        error_rate = (llm_errors / llm_calls) if llm_calls > 0 else 0.0

        def _avg(bucket: dict[str, int]) -> float:
            return (bucket["sum"] / bucket["count"]) if bucket["count"] > 0 else 0.0

        return {
            "llm_calls": llm_calls,
            "llm_errors": llm_errors,
            "llm_error_rate": error_rate,
            "tool_calls": tool_calls,
            "tool_errors": tool_errors,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "chat_qa": {
                "qa_router_latency_ms_avg": _avg(chat_qa_latency["qa_router"]),
                "synth_latency_ms_avg": _avg(chat_qa_latency["synth_answer"]),
                "e2e_latency_ms_avg": _avg(chat_qa_latency["e2e"]),
                "skill_latency_ms_avg": {
                    name: _avg(v) for name, v in skill_latency.items()
                },
                "skill_degraded_total": skill_degraded,
                "synth_degraded_total": synth_degraded,
                "escalation_total": chat_qa_escalations,
            },
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
            self._chat_qa_latency = {
                "qa_router": {"sum": 0, "count": 0},
                "synth_answer": {"sum": 0, "count": 0},
                "e2e": {"sum": 0, "count": 0},
            }
            self._skill_latency = {}
            self._skill_degraded = {}
            self._synth_degraded = 0
            self._chat_qa_escalations = {}


# 模块级单例：全局共享，回调 handler 与端点读取同一实例。
_collector = MetricsCollector()


def get_metrics_collector() -> MetricsCollector:
    """返回全局 MetricsCollector 单例。"""
    return _collector


def get_metrics() -> dict[str, object]:
    """返回当前累计指标快照（供 /metrics 端点）。"""
    return _collector.get_metrics()
