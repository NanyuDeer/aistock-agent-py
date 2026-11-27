"""步数耗尽后仍要产出 claim：thinking 兜底综合。"""

from __future__ import annotations

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.analysts.kernel import (
    AnalystResult,
    _astalled_synthesis,
    _StepBudgetMiddleware,
    _UsageBudgetCallback,
)

pytestmark = pytest.mark.asyncio


class _JsonModel(BaseChatModel):
    """只返回一段 JSON 的假模型。"""

    payload: str

    @property
    def _llm_type(self) -> str:
        return "fake-json"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        message = AIMessage(content=self.payload)
        message.usage_metadata = {  # type: ignore[attr-defined]
            "input_tokens": 10,
            "output_tokens": 20,
            "total_tokens": 30,
        }
        return ChatResult(generations=[ChatGeneration(message=message)])


def _stalled() -> AnalystResult:
    return AnalystResult(
        result_id="analyst_r0_lead",
        role="lead",
        research_round=0,
        claim="",
        confidence=0.4,
        status="stalled",
        error_kind="step_limit",
    )


async def test_stalled_result_gets_claim_from_thinking_synthesis() -> None:
    model = _JsonModel(
        payload='{"claim":"缩量下跌更可能来自风险偏好切换","confidence":0.62,'
        '"uncertainties":["缺少盘中资金流"],"requested_gaps":["gap_liquidity"]}'
    )
    budget = _StepBudgetMiddleware(8, 100000)
    callback = _UsageBudgetCallback(100000)

    updated = await _astalled_synthesis(
        _stalled(),
        thinking_model=model,
        task="hypotheses=hyp_liquidity:v1 evidence=ev_1",
        budget=budget,
        callback=callback,
    )

    assert updated.claim.startswith("缩量下跌")
    assert updated.confidence == pytest.approx(0.62)
    assert updated.status == "stalled"
    assert updated.error_kind == "step_limit"


async def test_synthesis_failure_keeps_original_result() -> None:
    model = _JsonModel(payload="not json")
    budget = _StepBudgetMiddleware(8, 100000)
    callback = _UsageBudgetCallback(100000)

    updated = await _astalled_synthesis(
        _stalled(),
        thinking_model=model,
        task="hypotheses=hyp_liquidity:v1",
        budget=budget,
        callback=callback,
    )

    assert updated.claim == ""
    assert updated.status == "stalled"
