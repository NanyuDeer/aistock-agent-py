"""P12-C：分析师拿到证据后追加一次启用 thinking 的终局推理（全部离线）。

根因：为绕开 `Thinking mode does not support this tool_choice` 关闭了 thinking，
牺牲了推理深度。方案：`build_thinking_model` 返回不关闭 thinking 的 deep 模型；
内核在结构化结果产出后追加一次无工具、不强制 tool_choice 的终局推理，用其
claim/confidence/uncertainties/requested_gaps；失败则回退原有结果。

本文件验证：
- `build_thinking_model` 调用 `get_deep_think` 且不注入 thinking=disabled；
- 合法 JSON → claim/confidence 被采用，合成 token 计入同一累计；
- 非 JSON → 保留原始结果且 status 不变。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.analysts import model_factory
from aistock_agent.brain_v4.analysts.kernel import AnalystResult, run_analyst_async
from aistock_agent.brain_v4.analysts.model_factory import build_thinking_model
from aistock_agent.brain_v4.graph.profiles import get_profile


def test_build_thinking_model_keeps_thinking_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sentinel = object()
    calls: list[dict[str, Any]] = []

    def fake_deep(**kwargs: Any) -> object:
        calls.append(kwargs)
        return sentinel

    monkeypatch.setattr(model_factory, "get_deep_think", fake_deep)

    model = build_thinking_model()

    assert model is sentinel
    # 不传 extra_body 即不关闭 thinking；与 build_analyst_model 的关闭逻辑分离。
    assert calls == [{"temperature": 0}]
    assert "extra_body" not in calls[0]


class _ThinkingFake(BaseChatModel):
    """同一替身同时服务 agent（结构化结果）与终局推理（严格 JSON）。"""

    synth_payload: str = (
        '{"claim": "thinking synthesized claim", "confidence": 0.78, '
        '"uncertainties": ["数据延迟"], "requested_gaps": ["缺口A"]}'
    )
    agent_tokens: int = 60
    synth_tokens: int = 40
    agent_calls: int = 0
    synth_calls: int = 0
    last_synth_prompt: str = ""

    @property
    def _llm_type(self) -> str:
        return "p12-thinking-fake"

    def bind_tools(
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _ThinkingFake:
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        joined = " ".join(str(message.content) for message in messages)
        if "终局推理" in joined:
            self.synth_calls += 1
            self.last_synth_prompt = joined
            return ChatResult(
                generations=[
                    ChatGeneration(
                        message=AIMessage(
                            content=self.synth_payload,
                            usage_metadata={
                                "input_tokens": 30,
                                "output_tokens": 10,
                                "total_tokens": self.synth_tokens,
                            },
                        )
                    )
                ]
            )
        self.agent_calls += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "AnalystResult",
                                "args": {
                                    "claim": "original claim",
                                    "evidence_refs": [],
                                    "confidence": 0.5,
                                },
                                "id": f"call_agent_{self.agent_calls}",
                                "type": "tool_call",
                            }
                        ],
                        usage_metadata={
                            "input_tokens": 40,
                            "output_tokens": 20,
                            "total_tokens": self.agent_tokens,
                        },
                    )
                )
            ]
        )


@pytest.mark.asyncio
async def test_thinking_synthesis_overrides_claim_and_confidence() -> None:
    profile = get_profile("market_trace")
    model = _ThinkingFake()

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=model,
        task="分析今日指数下跌主因",
        token_budget=profile.token_budget,
        thinking_model=model,
        timeout_s=10,
    )

    assert isinstance(result, AnalystResult)
    assert result.status == "ok"
    assert result.claim == "thinking synthesized claim"
    assert result.confidence == pytest.approx(0.78)
    assert result.uncertainties == ["数据延迟"]
    assert result.requested_gaps == ["缺口A"]
    assert model.synth_calls == 1
    # 合成消耗的 token 计入同一 callback 累计。
    assert result.tokens_used == (
        model.agent_tokens * model.agent_calls + model.synth_tokens * model.synth_calls
    )


@pytest.mark.asyncio
async def test_non_json_synthesis_falls_back_to_original_result() -> None:
    profile = get_profile("market_trace")
    model = _ThinkingFake(synth_payload="这不是 JSON")

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=model,
        task="分析今日指数下跌主因",
        token_budget=profile.token_budget,
        thinking_model=model,
        timeout_s=10,
    )

    assert isinstance(result, AnalystResult)
    assert result.status == "ok"
    assert result.claim == "original claim"
    assert result.confidence == pytest.approx(0.5)
    assert model.synth_calls == 1


@pytest.mark.asyncio
async def test_synthesis_context_is_compact_not_full_task() -> None:
    """合成不能再把整份任务（含超长证据正文）重发一遍。"""
    profile = get_profile("market_trace")
    long_body = "超长证据正文" * 800
    task = (
        "[hypotheses]\n"
        "- hyp_liquidity:v1 status=open confidence=0.60 | 流动性收缩是结构根因\n\n"
        "[evidence]\n"
        "- ev_snapshot_000 source=cls time=2026-11-27T09:00:00+08:00 "
        f"quality=high | {long_body}"
    )
    model = _ThinkingFake()

    result = await run_analyst_async(
        role="lead_thesis",
        profile=profile,
        model=model,
        task=task,
        token_budget=24000,
        thinking_model=model,
        timeout_s=10,
    )

    assert result.status == "ok"
    assert model.synth_calls == 1
    # 假设行保留，超长证据正文被截断，整体长度受控。
    assert "hyp_liquidity" in model.last_synth_prompt
    assert len(model.last_synth_prompt) < 3000
    assert long_body[:1000] not in model.last_synth_prompt


@pytest.mark.asyncio
async def test_synthesis_skipped_when_remaining_budget_insufficient() -> None:
    """剩余额度不够时直接跳过合成，不发起请求、也不丢主循环结论。"""
    profile = get_profile("market_trace")
    model = _ThinkingFake(agent_tokens=400)

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=model,
        task="[hypotheses]\n- hyp_broad:v1 | 广度驱动",
        token_budget=700,
        thinking_model=model,
        timeout_s=10,
    )

    assert isinstance(result, AnalystResult)
    assert result.status == "ok"
    assert result.claim == "original claim"
    assert model.synth_calls == 0


@pytest.mark.asyncio
async def test_no_thinking_model_skips_synthesis() -> None:
    profile = get_profile("market_trace")
    model = _ThinkingFake()

    result = await run_analyst_async(
        role="quant",
        profile=profile,
        model=model,
        task="分析今日指数下跌主因",
        token_budget=profile.token_budget,
        timeout_s=10,
    )

    assert result.status == "ok"
    assert result.claim == "original claim"
    assert model.synth_calls == 0
