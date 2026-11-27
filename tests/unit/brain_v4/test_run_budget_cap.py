"""P9：run 级 token 预算硬上限（全部离线）。

缺陷：`profile.token_budget=32000`，但预算只在单个分析师维度检查，
research 循环每轮把整份预算传给每个席位，累计消耗可远超预算后才出现
`research_budget_exhausted`（服务器实测烧掉 62 万 token）。

本文件用固定 5000 tokens/次的 `FakeChatModel` + 记录型 `ToolClient` 跑
`build_research_graph`，断言：
- 图返回的累计 `analyst_tokens` 不超过 `profile.token_budget * 1.2`；
- 预算不足以支撑新一轮时不再新增 analyst 模型调用（调用次数有界）；
- `stop_reason` 含 `research_budget_exhausted`，且图正常收敛出 judgment。

不触网、不调真实 LLM、不读系统时间。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.analysts.profiles import analyst_seats
from aistock_agent.brain_v4.contracts.deliberation import CaseFile
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest
from aistock_agent.brain_v4.graph.builder import build_research_graph
from aistock_agent.brain_v4.graph.profiles import get_profile
from aistock_agent.brain_v4.tools.gateway import ToolClientResult

AS_OF = datetime.fromisoformat("2026-11-27T15:35:00+08:00")
DATA_CUTOFF = datetime.fromisoformat("2026-11-27T15:00:00+08:00")
DEADLINE = datetime.fromisoformat("2026-11-27T16:35:00+08:00")

REF_ALPHA = "hyp_alpha:v1"
REF_BETA = "hyp_beta:v1"

TOKENS_PER_CALL = 5000


class _FixedUsageModel(BaseChatModel):
    """确定性假模型：每次调用固定回报 ``TOKENS_PER_CALL`` tokens。

    为什么只回报一次模型调用：`ToolStrategy(AnalystResult)` 在结构化工具调用
    被捕获后即结束，一个分析师实例只产生一次 model call；因此累计 token 就是
    派发席位数 × 每次 token，便于精确验证 run 级预算封顶。
    """

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "p9-fixed-usage-fake"

    def bind_tools(
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _FixedUsageModel:
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
                                    "claim": "p9 structured claim",
                                    "evidence_refs": [],
                                    "confidence": 0.55,
                                    "uncertainties": [],
                                    "requested_gaps": [],
                                },
                                "id": f"call_p9_{self.calls}",
                                "type": "tool_call",
                            }
                        ],
                        usage_metadata={
                            "input_tokens": 3000,
                            "output_tokens": 2000,
                            "total_tokens": TOKENS_PER_CALL,
                        },
                    )
                )
            ]
        )


class _RecordingToolClient:
    """记录型假 ToolClient：记录调用并返回真实形状的行情载荷。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult:
        self.calls.append((tool, dict(params)))
        return ToolClientResult(
            data={"tool": tool, "indexes": {"000001.SH": {"close": 3012.4}}},
            published_at=DATA_CUTOFF,
            event_time=DATA_CUTOFF,
            quality="high",
        )


def _request() -> ResearchRequest:
    return ResearchRequest(
        request_id="req_p9_budget",
        run_id="run_p9_budget",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        research_goal="Which root cause explains today's index move?",
        required_questions=[],
        horizon="1d",
        as_of=AS_OF,
        data_cutoff=DATA_CUTOFF,
        snapshot_id="snap_p9_budget",
        config_version="cfg_v1",
        model_version="model_v1",
        prompt_version="prompt_v1",
        method_version="method_v1",
        budget_policy="market_trace",
        stop_policy="default",
    )


def _case_file() -> CaseFile:
    # 低材料性 + 不接近的假设分差 -> 不会启动议会/红队，便于单写研判收敛。
    return CaseFile(
        case_id="case_p9_budget",
        run_id="run_p9_budget",
        profile="market_trace",
        case_version=1,
        question="Which root cause explains today's index move?",
        decision_type="market_root_cause",
        materiality="low",
        subject="market",
        horizon="1d",
        as_of=AS_OF,
        snapshot_id="snap_p9_budget",
        frozen_fact_ids=[],
        hypothesis_refs=[REF_ALPHA, REF_BETA],
        open_issue_ids=["issue_hyp_alpha", "issue_hyp_beta"],
        deadline=DEADLINE,
        budget_policy="market_trace",
        decision_rubric_version="rubric_v1",
    )


def _hypotheses() -> list[Hypothesis]:
    # 故意要求一种证据来源永远拿不到（工具证据 source_type 是工具组名），
    # 保证每轮充分性门禁都失败，从而真实进入“新一轮派发”的预算判定。
    return [
        Hypothesis(
            hypothesis_id="hyp_alpha",
            version=1,
            statement="第一竞争解释",
            status="open",
            prior_confidence=0.60,
            assumptions=["证据 A"],
            disconfirmers=["证据 A 反向"],
            required_evidence_types=["market_fact"],
            scope="market",
        ),
        Hypothesis(
            hypothesis_id="hyp_beta",
            version=1,
            statement="第二竞争解释",
            status="open",
            prior_confidence=0.20,
            assumptions=["证据 B"],
            disconfirmers=["证据 B 反向"],
            required_evidence_types=["market_fact"],
            scope="market",
        ),
    ]


def _initial_state() -> dict[str, Any]:
    return {
        "request": _request(),
        "case_file": _case_file(),
        "evidence_ledger": EvidenceLedger(
            run_id="run_p9_budget",
            snapshot_id="snap_p9_budget",
            as_of=AS_OF,
            items=[],
            conflict_ids=[],
        ),
        "hypotheses": _hypotheses(),
        "causal_graph": [],
        "research_gaps": [],
        "publish_ready": False,
        "counter_evidence_coverage": 1.0,
        "parent_judgment_ids": [],
        "usage": [],
        "errors": [],
        "trace": [],
    }


def _usage(state: Mapping[str, Any], key: str) -> float:
    total = 0.0
    for record in state.get("usage", ()):
        if isinstance(record, Mapping) and record.get("key") == key:
            value = record.get("value")
            if isinstance(value, int | float) and not isinstance(value, bool):
                total += float(value)
    return total


@pytest.mark.asyncio
async def test_run_level_token_budget_caps_dispatch_and_stops() -> None:
    profile = get_profile("market_trace")
    seats = analyst_seats(profile)
    assert len(seats) > 1, "用例依赖多席位以验证 run 级累计预算"

    model = _FixedUsageModel()
    client = _RecordingToolClient()
    graph = build_research_graph(tool_client=client, analyst_model=model)

    final: dict[str, Any] = await graph.ainvoke(
        _initial_state(), config={"recursion_limit": 300}
    )

    used = _usage(final, "analyst_tokens")
    # 图必须正常收敛出研判，而不是抛错/半途中断。
    assert final.get("judgment_draft") is not None

    # 1) 累计消耗不得超过预算的 1.2 倍。
    assert used > 0
    assert used <= profile.token_budget * 1.2

    # 2) 预算不足以支撑新一轮后，不再新增 analyst 模型调用：
    #    只有第一轮派发的席位会调用模型（每席位固定 1 次）。
    assert model.calls == len(seats)
    assert used == model.calls * TOKENS_PER_CALL

    # 3) 停止原因显式标记为预算耗尽。
    assert final["judgment_draft"].stop_reason == "research_budget_exhausted"
