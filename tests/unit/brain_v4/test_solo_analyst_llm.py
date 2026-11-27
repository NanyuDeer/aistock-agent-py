"""P7：SOLO_DEEP 路径启用主分析师（全部离线）。

背景（服务器真实运行 + 探针）：`market_trace` 的确定性路由会落到
`SOLO_DEEP`，但旧 `solo_node` 只写 `run_status="solo"`，从不派发主分析师，
导致该路径一次真实 LLM 都没调用（`usage.total_tokens=0`）。本文件验证：

- 注入真实形状的 FakeChatModel 后，SOLO 路径确实产出分析师结果与真实
  token 用量（`analyst_tokens > 0`）；
- 模型为 None 时保持既有确定性行为：不产生任何模型 token；
- 分析师异常被 `solo_node` 收敛，不把图打崩，trace 留失败标记。

说明：`analyst_seat` 扇出在研究循环里始终先于协作路由执行，因此最终
`run_status` 会被 `decision_gate` 改写为研判状态。这里用协作模式
（`SOLO_DEEP`）与 `solo` 节点 trace 来定位“主分析师”这一步，而不是看
最终 `run_status`。全部离线、不触网、不调真实 LLM、不读系统时间。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from aistock_agent.brain_v4.contracts.deliberation import CaseFile
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest
from aistock_agent.brain_v4.graph import builder as builder_module
from aistock_agent.brain_v4.graph.builder import build_research_graph

AS_OF = datetime.fromisoformat("2026-11-27T10:00:00+08:00")
DATA_CUTOFF = datetime.fromisoformat("2026-11-27T09:30:00+08:00")
DEADLINE = datetime.fromisoformat("2026-11-27T15:00:00+08:00")

REF_ALPHA = "hyp_alpha:v1"
REF_BETA = "hyp_beta:v1"


class _StructuredAnalystModel(BaseChatModel):
    """确定性假模型：直接产出 `AnalystResult` 工具调用并回报真实 token 用量。"""

    calls: int = 0

    @property
    def _llm_type(self) -> str:
        return "p7-solo-structured-fake"

    def bind_tools(
        self, tools: Sequence[Any], **kwargs: Any
    ) -> _StructuredAnalystModel:
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
                                    "claim": "solo structured claim",
                                    "evidence_refs": [],
                                    "confidence": 0.55,
                                    "uncertainties": [],
                                    "requested_gaps": [],
                                },
                                "id": f"call_solo_{self.calls}",
                                "type": "tool_call",
                            }
                        ],
                        usage_metadata={
                            "input_tokens": 35,
                            "output_tokens": 25,
                            "total_tokens": 60,
                        },
                    )
                )
            ]
        )


def _request() -> ResearchRequest:
    return ResearchRequest(
        request_id="req_p7_solo",
        run_id="run_p7_solo",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        research_goal="Which root cause explains today's index move?",
        required_questions=[],
        horizon="1d",
        as_of=AS_OF,
        data_cutoff=DATA_CUTOFF,
        snapshot_id="snap_p7_solo",
        config_version="cfg_v1",
        model_version="model_v1",
        prompt_version="prompt_v1",
        method_version="method_v1",
        budget_policy="market_trace",
        stop_policy="default",
    )


def _case_file() -> CaseFile:
    # 低材料性 + 无冲突 -> 确定性路由落入 SOLO_DEEP。
    return CaseFile(
        case_id="case_p7_solo",
        run_id="run_p7_solo",
        profile="market_trace",
        case_version=1,
        question="Which root cause explains today's index move?",
        decision_type="market_root_cause",
        materiality="low",
        subject="market",
        horizon="1d",
        as_of=AS_OF,
        snapshot_id="snap_p7_solo",
        frozen_fact_ids=[],
        hypothesis_refs=[REF_ALPHA, REF_BETA],
        open_issue_ids=["issue_hyp_alpha", "issue_hyp_beta"],
        deadline=DEADLINE,
        budget_policy="market_trace",
        decision_rubric_version="rubric_v1",
    )


def _hypotheses() -> list[Hypothesis]:
    return [
        Hypothesis(
            hypothesis_id="hyp_alpha",
            version=1,
            statement="第一竞争解释",
            status="open",
            prior_confidence=0.60,
            assumptions=["证据 A"],
            disconfirmers=["证据 A 反向"],
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
            scope="market",
        ),
    ]


def _initial_state() -> dict[str, Any]:
    return {
        "request": _request(),
        "case_file": _case_file(),
        "evidence_ledger": EvidenceLedger(
            run_id="run_p7_solo",
            snapshot_id="snap_p7_solo",
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


def _solo_traces(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        dict(entry)
        for entry in state.get("trace", ())
        if isinstance(entry, Mapping) and entry.get("node") == "solo"
    ]


@pytest.mark.asyncio
async def test_solo_path_calls_deep_analyst_with_real_token_usage() -> None:
    model = _StructuredAnalystModel()
    graph = build_research_graph(analyst_model=model)

    final: dict[str, Any] = await graph.ainvoke(
        _initial_state(), config={"recursion_limit": 200}
    )

    # 确定性路由确实落到 SOLO_DEEP，solo 节点确实用模型跑了一次主分析师。
    assert final["collaboration_decision"].mode == "SOLO_DEEP"
    solo_traces = _solo_traces(final)
    assert solo_traces, "solo 节点未执行"
    assert solo_traces[0].get("analyst_result_id"), "solo 未派发主分析师"
    assert solo_traces[0].get("analyst_tokens", 0) > 0

    assert final["analyst_results"]
    assert _usage(final, "analyst_tokens") > 0
    assert model.calls > 0


@pytest.mark.asyncio
async def test_solo_path_without_model_stays_deterministic() -> None:
    graph = build_research_graph(analyst_model=None)

    final: dict[str, Any] = await graph.ainvoke(
        _initial_state(), config={"recursion_limit": 200}
    )

    # 无模型时 solo 保持既有确定性：不产生任何模型 token，也不新增主分析师结果。
    assert final["collaboration_decision"].mode == "SOLO_DEEP"
    assert _usage(final, "analyst_tokens") == 0
    solo_traces = _solo_traces(final)
    assert solo_traces
    assert "analyst_result_id" not in solo_traces[0]


@pytest.mark.asyncio
async def test_solo_analyst_failure_does_not_crash_graph(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real = builder_module.run_analyst_async

    async def _boom(**kwargs: Any) -> Any:
        # 只让 solo 节点的调用抛异常；席位扇出仍走真实内核。
        if "SOLO_DEEP" in str(kwargs.get("task", "")):
            raise RuntimeError("solo model exploded")
        return await real(**kwargs)

    monkeypatch.setattr(builder_module, "run_analyst_async", _boom)

    graph = build_research_graph(analyst_model=_StructuredAnalystModel())
    final: dict[str, Any] = await graph.ainvoke(
        _initial_state(), config={"recursion_limit": 200}
    )

    # 图没有被异常打崩：仍完成研判，并在 trace/usage 留下失败标记。
    assert final.get("judgment_draft") is not None
    solo_traces = _solo_traces(final)
    assert solo_traces
    assert solo_traces[0].get("analyst_failed") is True
    assert _usage(final, "analyst_error") == 1
