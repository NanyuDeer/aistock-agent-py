"""token 用量采集 — callback 层写入，synth_answer 收口到 state（P10 线 2）。

为什么用 contextvar 而非 state 传参：
- TokenUsageCallback 挂在 ChatOpenAI 的 callbacks= 上（services/llm.py），
  回调层无法访问节点 state；
- contextvar 随 asyncio.create_task 继承：ws 后台图任务（routes.py
  _run_graph_to_queue）与节点内部 LLM 调用都发生在同一 context 副本，
  record 与读取天然对齐，无需在节点间层层传递。
- 非 chat 场景（worker 独立运行）也会触发 on_llm_end：record 自动创建
  累加器，无读取方时零副作用（不报错）。
"""
from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass
class TokenUsageAccumulator:
    """单轮 token 用量累加器（一条对话消息内多次 LLM 调用累计）。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    def add(self, prompt: int, completion: int, total: int) -> None:
        """累加一次 LLM 调用的三项 token 数。"""
        self.prompt_tokens += prompt
        self.completion_tokens += completion
        self.total_tokens += total

    def snapshot(self) -> dict[str, int] | None:
        """返回当前快照；全 0 返回 None（无 LLM 用量不产出计费记录）。"""
        if self.prompt_tokens == 0 and self.completion_tokens == 0 and self.total_tokens == 0:
            return None
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


_usage_var: ContextVar[TokenUsageAccumulator | None] = ContextVar(
    "chat_token_usage", default=None
)


class TokenUsageContext:
    """模块级 contextvar 访问点（统一命名空间，便于测试定位/调试）。"""

    @staticmethod
    def reset_token_usage() -> None:
        """开启新一轮采集（ws.py/routes.py 每条消息入口调用）。"""
        acc = TokenUsageAccumulator()
        _usage_var.set(acc)

    @staticmethod
    def get_token_usage() -> dict[str, int] | None:
        """读取当前累加器快照；未设置或全 0 返回 None。"""
        acc = _usage_var.get()
        return acc.snapshot() if acc is not None else None

    @staticmethod
    def record_token_usage(prompt: int, completion: int, total: int) -> None:
        """记录一次 LLM 调用用量；无累加器时自动创建（非 chat 场景不报错）。"""
        acc = _usage_var.get()
        if acc is None:
            acc = TokenUsageAccumulator()
            _usage_var.set(acc)
        acc.add(prompt, completion, total)


# 模块级便捷函数：callback / synth_answer / 路由共用同一 contextvar
reset_token_usage = TokenUsageContext.reset_token_usage
get_token_usage = TokenUsageContext.get_token_usage
record_token_usage = TokenUsageContext.record_token_usage
