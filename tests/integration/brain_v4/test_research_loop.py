"""V4 主图两轮研究循环与受限分析师内核集成测试（Task 6）。

覆盖：
- 研究第 1 轮产生结构化 Gap，第 2 轮消除 Gap 并选出议会模式；
- 议会 R0 先产出独立立场，R1 记录 PeerBrief 与 BeliefUpdate 后才允许
  `commit_judgment`；
- 研究轮次计数与议会轮次计数分离；
- trace 同时含研究/议会两类 ledger，同一工具同参数不重复触达客户端；
- 全流程离线确定性，不触网、不读系统时间；
- 内核超步数返回结构化 stalled，研判单写拒绝账本外证据。
"""

import asyncio
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool

from aistock_agent.brain_v4.analysts.kernel import AnalystResult, run_analyst
from aistock_agent.brain_v4.contracts.deliberation import CaseFile, CollaborationDecision
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchRequest,
)
from aistock_agent.brain_v4.graph.builder import (
    JudgmentEvidenceError,
    build_research_graph,
    commit_judgment,
)
from aistock_agent.brain_v4.graph.profiles import get_profile
from aistock_agent.brain_v4.tools.gateway import ToolClientResult

AS_OF = datetime.fromisoformat("2026-11-27T10:00:00+08:00")
DATA_CUTOFF = datetime.fromisoformat("2026-11-27T09:30:00+08:00")
EVENT_TIME = datetime.fromisoformat("2026-11-27T09:00:00+08:00")
PUBLISHED_AT = datetime.fromisoformat("2026-11-27T09:05:00+08:00")
VALID_TIME = datetime.fromisoformat("2026-11-27T09:00:00+08:00")
DEADLINE = datetime.fromisoformat("2026-11-27T15:00:00+08:00")

LIQUIDITY_REF = "hyp_liquidity:v1"
RISK_REF = "hyp_risk_pref:v1"


def _request() -> ResearchRequest:
    return ResearchRequest(
        request_id="req_research_loop",
        run_id="run_research_loop",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        research_goal="Which root cause explains today's index move?",
        required_questions=[],
        horizon="1d",
        as_of=AS_OF,
        data_cutoff=DATA_CUTOFF,
        snapshot_id="snap_research_loop",
        config_version="cfg_v1",
        model_version="model_v1",
        prompt_version="prompt_v1",
        method_version="method_v1",
        budget_policy="market_trace",
        stop_policy="default",
    )


def _case_file() -> CaseFile:
    return CaseFile(
        case_id="case_research_loop",
        run_id="run_research_loop",
        profile="market_trace",
        case_version=1,
        question="Which root cause explains today's index move?",
        decision_type="market_root_cause",
        materiality="high",
        subject="market",
        horizon="1d",
        as_of=AS_OF,
        snapshot_id="snap_research_loop",
        frozen_fact_ids=[],
        hypothesis_refs=[LIQUIDITY_REF, RISK_REF],
        open_issue_ids=["issue_hyp_liquidity", "issue_hyp_risk_pref"],
        deadline=DEADLINE,
        budget_policy="market_trace",
        decision_rubric_version="rubric_v1",
    )


def _hypotheses() -> list[Hypothesis]:
    return [
        Hypothesis(
            hypothesis_id="hyp_liquidity",
            version=1,
            statement="流动性收缩是主因",
            status="open",
            prior_confidence=0.56,
            assumptions=["北向资金净流出"],
            disconfirmers=["北向资金转为净流入"],
            scope="market",
        ),
        Hypothesis(
            hypothesis_id="hyp_risk_pref",
            version=1,
            statement="风险偏好回落是主因",
            status="open",
            prior_confidence=0.51,
            assumptions=["高 beta 板块领跌"],
            disconfirmers=["高 beta 板块逆势上涨"],
            scope="market",
        ),
    ]


def _causal_edges() -> list[CausalEdge]:
    return [
        CausalEdge(
            edge_id="edge_liquidity_index",
            from_node="hyp_liquidity",
            to_node="index_move",
            mechanism="流动性收缩压缩估值",
            supporting_evidence_ids=["ev_liquidity_fixture"],
            edge_type="inference",
            confidence=0.55,
            valid_time=VALID_TIME,
            magnitude_check="指数下跌 1.2%",
            counterfactual_check="若北向净流入则指数不应下跌",
        ),
        CausalEdge(
            edge_id="edge_risk_index",
            from_node="hyp_risk_pref",
            to_node="index_move",
            mechanism="风险偏好回落压制高 beta",
            supporting_evidence_ids=["ev_risk_fixture"],
            edge_type="inference",
            confidence=0.5,
            valid_time=VALID_TIME,
            magnitude_check="高 beta 板块下跌 2.1%",
            counterfactual_check="若高 beta 领涨则风险偏好未回落",
        ),
    ]


def _initial_ledger() -> EvidenceLedger:
    return EvidenceLedger(
        run_id="run_research_loop",
        snapshot_id="snap_research_loop",
        as_of=AS_OF,
        items=[],
        conflict_ids=["conflict_liquidity_vs_risk_pref"],
    )


class CountingToolClient:
    """离线计数客户端：记录每次真实触达，并返回按轮次变化的证据。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def fetch(self, tool: str, params: dict[str, Any]) -> ToolClientResult:
        self.calls.append((tool, json.dumps(params, sort_keys=True, ensure_ascii=False)))
        round_no = int(params["round_no"])
        supports = (
            [LIQUIDITY_REF]
            if round_no == 0
            else [LIQUIDITY_REF, RISK_REF]
        )
        return ToolClientResult(
            data={"supports": supports, "round_no": round_no},
            event_time=EVENT_TIME,
            published_at=PUBLISHED_AT,
            quality="high",
        )


class StructuredAnalystModel(BaseChatModel):
    """每次调用都直接返回 `AnalystResult` 工具调用的假模型。"""

    @property
    def _llm_type(self) -> str:
        return "structured-analyst-fake"

    def bind_tools(
        self, tools: Sequence[BaseTool | dict[str, Any]], **kwargs: Any
    ) -> "StructuredAnalystModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "AnalystResult",
                                "args": {
                                    "claim": "reviewed structured evidence",
                                    "evidence_refs": [],
                                    "confidence": 0.6,
                                    "uncertainties": [],
                                    "requested_gaps": [],
                                },
                                "id": "call_structured_analyst",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def _initial_state() -> dict[str, Any]:
    return {
        "request": _request(),
        "case_file": _case_file(),
        "evidence_ledger": _initial_ledger(),
        "hypotheses": _hypotheses(),
        "causal_graph": _causal_edges(),
        "research_gaps": [],
        "publish_ready": False,
        "counter_evidence_coverage": 1.0,
        "parent_judgment_ids": [],
        "usage": [],
        "errors": [],
        "trace": [],
    }


async def _run_research_loop() -> tuple[dict[str, Any], CountingToolClient]:
    client = CountingToolClient()
    graph = build_research_graph(
        tool_client=client,
        analyst_model=StructuredAnalystModel(),
    )
    final: dict[str, Any] = await graph.ainvoke(
        _initial_state(), config={"recursion_limit": 200}
    )
    return final, client


def test_two_round_research_loop_selects_council_and_commits_judgment():
    final, client = asyncio.run(_run_research_loop())

    # 研究第 1 轮产生 gap，第 2 轮消除 gap。
    ledgers = sorted(final["round_ledgers"], key=lambda item: item.round_no)
    assert [ledger.round_no for ledger in ledgers] == [0, 1]
    assert ledgers[0].sufficiency_passed is False
    assert ledgers[0].unresolved_gaps
    assert ledgers[1].sufficiency_passed is True
    assert ledgers[1].unresolved_gaps == []

    # 研究轮次计数与议会轮次计数分离。
    assert final["research_round_no"] == 1
    deliberation_round = max(
        ledger.deliberation_round
        for ledger in final["deliberation_round_ledgers"]
    )
    assert final["deliberation_round_no"] == deliberation_round
    assert deliberation_round >= 1

    # 选中议会：R0 先独立立论，R1 记录 PeerBrief 与 BeliefUpdate。
    assert final["collaboration_decision"].mode == "ANALYST_COUNCIL"
    assert any(
        paper.position_id.startswith("pos_r00") for paper in final["submissions"]
    )
    assert any(brief.deliberation_round >= 1 for brief in final["peer_briefs"])
    assert any(update.deliberation_round >= 1 for update in final["belief_updates"])

    # judgment 单写完成，且引用证据全部在冻结账本内。
    judgment = final["judgment_draft"]
    ledger_ids = {item.evidence_id for item in final["evidence_ledger"].items}
    assert set(judgment.evidence_ids) <= ledger_ids
    assert judgment.as_of == AS_OF
    assert judgment.snapshot_id == "snap_research_loop"

    # trace 同时含两类 ledger。
    ledger_types = {entry.get("ledger_type") for entry in final["trace"]}
    assert "research" in ledger_types
    assert "deliberation" in ledger_types

    # 同一工具同参数只触达一次客户端：4 个工具组 × 2 轮。
    keys = [f"{tool}|{params}" for tool, params in client.calls]
    assert len(keys) == len(set(keys))
    assert len(client.calls) == 8


class LoopingToolModel(BaseChatModel):
    """永远调用同一个工具、从不返回结构化结果的假模型。"""

    counter: int = 0

    @property
    def _llm_type(self) -> str:
        return "looping-tool-fake"

    def bind_tools(
        self, tools: Sequence[BaseTool | dict[str, Any]], **kwargs: Any
    ) -> "LoopingToolModel":
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.counter += 1
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "probe_tool",
                                "args": {"n": self.counter},
                                "id": f"call_probe_{self.counter}",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


def probe_tool(n: int) -> str:
    """确定性命中计数工具。"""
    return f"pong {n}"


def test_analyst_kernel_returns_stalled_at_step_ceiling():
    profile = get_profile("alert_hot")
    result = run_analyst(
        role="quant",
        profile=profile,
        model=LoopingToolModel(),
        tools=[probe_tool],
        task="不断调用工具，验证步数上限",
    )
    assert isinstance(result, AnalystResult)
    assert result.status == "stalled"
    assert result.error_kind == "step_limit"
    assert result.steps_used == profile.analyst_max_steps
    assert result.max_steps == profile.analyst_max_steps
    assert result.claim == ""


def _solo_decision() -> CollaborationDecision:
    return CollaborationDecision(
        mode="SOLO_DEEP",
        trigger_codes=["narrow_question"],
        selected_roles=[],
        lead_role="lead_thesis",
        judge_role="council_judge",
        max_objection_rounds=0,
        max_rebuttal_rounds=0,
        human_required=False,
    )


def _judgment_state(*, evidence_refs: list[str]) -> dict[str, Any]:
    return {
        "request": _request(),
        "case_file": _case_file(),
        "evidence_ledger": _initial_ledger(),
        "hypotheses": _hypotheses(),
        "collaboration_decision": _solo_decision(),
        "analyst_results": [
            AnalystResult(
                result_id="analyst_r00_lead_thesis",
                role="lead_thesis",
                claim="draft",
                evidence_refs=evidence_refs,
            )
        ],
        "sufficiency_passed": True,
        "stop_reason": "stall",
        "research_round_no": 0,
    }


def test_judgment_rejects_evidence_outside_ledger():
    state = _judgment_state(evidence_refs=["ev_missing_from_ledger"])
    with pytest.raises(JudgmentEvidenceError):
        commit_judgment(state)


def test_judgment_status_and_stop_reason_are_independent():
    state = _judgment_state(evidence_refs=[])
    judgment = commit_judgment(state)
    assert judgment.status == "ok"
    assert judgment.stop_reason == "stall"
