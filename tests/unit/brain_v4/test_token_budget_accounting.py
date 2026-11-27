"""P12-A：真实 token 用量计数与 run 内硬上限（全部离线）。

根因（服务器真实运行）：`_StepBudgetMiddleware._record_tokens` 依赖
`response.result` 里的 `usage_metadata`，真实 ChatOpenAI/DeepSeek 路径下从未
累计到，`tokens_used` 长期为 0，预算形同虚设。

本文件验证：
- `_UsageBudgetCallback` 直接从 `LLMResult` 提取真实 usage（优先
  `llm_output.token_usage`，否则遍历 `generations[*].message.usage_metadata`），
  不依赖中间件；
- 经 `run_analyst_async` 注入后，`AnalystResult.tokens_used` 等于真实累计值；
- 累计超过预算 1.2 倍硬上限时返回结构化 `stalled/budget_exhausted`，不抛裸异常。

不触网、不调用真实 LLM、不读系统时间。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult, LLMResult

from aistock_agent.brain_v4.analysts.kernel import (
    AnalystResult,
    TokenBudgetExceededError,
    _UsageBudgetCallback,
    run_analyst_async,
)
from aistock_agent.brain_v4.graph.profiles import get_profile


def _llm_result_with_usage(total: int) -> LLMResult:
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        usage_metadata={
                            "input_tokens": max(total - 20, 0),
                            "output_tokens": min(total, 20),
                            "total_tokens": total,
                        },
                    )
                )
            ]
        ]
    )


def test_callback_counts_usage_metadata_from_generations() -> None:
    callback = _UsageBudgetCallback(hard_limit=10_000)

    callback.on_llm_end(_llm_result_with_usage(50))

    assert callback.tokens_used == 50


def test_callback_prefers_llm_output_token_usage() -> None:
    callback = _UsageBudgetCallback(hard_limit=10_000)
    response = LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        usage_metadata={
                            "input_tokens": 1,
                            "output_tokens": 1,
                            "total_tokens": 2,
                        },
                    )
                )
            ]
        ],
        llm_output={"token_usage": {"total_tokens": 77}},
    )

    callback.on_llm_end(response)

    assert callback.tokens_used == 77


def test_callback_raises_when_hard_limit_exceeded() -> None:
    callback = _UsageBudgetCallback(hard_limit=40)

    with pytest.raises(TokenBudgetExceededError):
        callback.on_llm_end(_llm_result_with_usage(50))


class _UsageModel(BaseChatModel):
    """确定性假模型：每次调用回报固定真实 usage，并直接产出结构化结果。"""

    total_tokens: int = 120
    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "p12-usage-fake"

    def bind_tools(
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _UsageModel:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.calls += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "AnalystResult",
                                "args": {
                                    "claim": "usage counted claim",
                                    "evidence_refs": [],
                                    "confidence": 0.55,
                                },
                                "id": f"call_usage_{self.calls}",
                                "type": "tool_call",
                            }
                        ],
                        usage_metadata={
                            "input_tokens": max(self.total_tokens - 20, 0),
                            "output_tokens": min(self.total_tokens, 20),
                            "total_tokens": self.total_tokens,
                        },
                    )
                )
            ]
        )


@pytest.mark.asyncio
async def test_analyst_result_tokens_used_tracks_real_usage() -> None:
    profile = get_profile("market_trace")
    model = _UsageModel(total_tokens=120)

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=model,
        task="统计真实 token 用量",
        token_budget=profile.token_budget,
        timeout_s=10,
    )

    assert isinstance(result, AnalystResult)
    assert result.status == "ok"
    assert model.calls >= 1
    # callback 累计每次真实模型调用的 usage，不依赖中间件 response.result 路径。
    assert result.tokens_used == 120 * model.calls


@pytest.mark.asyncio
async def test_tokens_over_hard_limit_stop_with_budget_exhausted() -> None:
    profile = get_profile("alert_hot")
    model = _UsageModel(total_tokens=6000)

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=model,
        task="验证硬上限",
        token_budget=profile.token_budget,
        timeout_s=10,
    )

    assert isinstance(result, AnalystResult)
    assert result.status == "stalled"
    assert result.error_kind == "budget_exhausted"
    # 硬上限是预算的 1.2 倍：超限即停，用量如实记到真实回报。
    assert result.tokens_used >= int(profile.token_budget * 1.2)
