"""P8-B：research_task 必须把工具组展开为原子工具，且只传白名单参数。

服务器日志（2026-11-27）20 条 error：
- ``tool 'fundamental'/'event_evidence'/'trend' is not allowed by the V4 tool map``
- ``tool 'market_snapshot' got unexpected params: round_no, subject_code, tool``

根因：``graph/builder.py`` 的 research_task 把工具组名当工具名，并把
``round_no/subject_code/tool`` 等编排字段塞进工具参数。本测试用记录型假网关
确认：传入网关的工具名是 TOOL_MAP 原子工具，参数键集合是该工具白名单子集，
``fundamental`` 展开为空组时完全不产生调用。

全部离线：假网关不触网，模型为 None（确定性分析师桩）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from aistock_agent.brain_v4.contracts.deliberation import CaseFile
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchRequest,
)
from aistock_agent.brain_v4.graph.builder import build_research_graph
from aistock_agent.brain_v4.tools.provenance import ToolEnvelope
from aistock_agent.brain_v4.tools.tool_map import TOOL_MAP

AS_OF = datetime.fromisoformat("2026-11-27T10:00:00+08:00")
DATA_CUTOFF = datetime.fromisoformat("2026-11-27T09:30:00+08:00")
VALID_TIME = datetime.fromisoformat("2026-11-27T09:00:00+08:00")
DEADLINE = datetime.fromisoformat("2026-11-27T15:00:00+08:00")

LIQUIDITY_REF = "hyp_liquidity:v1"
RISK_REF = "hyp_risk_pref:v1"

#: market_trace 的 4 个工具组展开后应出现的全部原子工具。
_EXPECTED_ATOMS = {
    "market_snapshot",
    "intraday_sectors",
    "calendar_events",
    "ths_index_map",
    "index_kline",
}
#: 编排层组名，绝不能出现在网关调用里。
_FORBIDDEN_GROUPS = {"market_snapshot_group", "event_evidence", "trend", "fundamental"}


class _RecordingGateway:
    """记录型假网关：返回成功信封，不触网。"""

    def __init__(
        self,
        client: object = None,
        *,
        as_of: object = None,
        allowed_tools: object = None,
        timeout_s: float = 10.0,
        clock: object = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, params: dict[str, Any]) -> ToolEnvelope:
        self.calls.append((tool, dict(params)))
        return ToolEnvelope(
            tool=tool,
            params=dict(params),
            evidence_id=f"ev_{len(self.calls):04d}",
            event_time=DATA_CUTOFF,
            published_at=DATA_CUTOFF,
            fetched_at=AS_OF,
            as_of=AS_OF,
            quality="high",
            error=None,
            error_detail=None,
            # supports 显式给出两个假设，保证离线研究循环能判定充分。
            data={"supports": [LIQUIDITY_REF, RISK_REF]},
        )


def _request() -> ResearchRequest:
    return ResearchRequest(
        request_id="req_p8_tools",
        run_id="run_p8_tools",
        profile="market_trace",
        subject_type="market",
        subject_code="000001.SH",
        research_goal="Which root cause explains today's index move?",
        required_questions=[],
        horizon="1d",
        as_of=AS_OF,
        data_cutoff=DATA_CUTOFF,
        snapshot_id="snap_p8_tools",
        config_version="cfg_v1",
        model_version="model_v1",
        prompt_version="prompt_v1",
        method_version="method_v1",
        budget_policy="market_trace",
        stop_policy="default",
    )


def _case_file() -> CaseFile:
    return CaseFile(
        case_id="case_p8_tools",
        run_id="run_p8_tools",
        profile="market_trace",
        case_version=1,
        question="Which root cause explains today's index move?",
        decision_type="market_root_cause",
        materiality="high",
        subject="market",
        horizon="1d",
        as_of=AS_OF,
        snapshot_id="snap_p8_tools",
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
        )
    ]


def _initial_state() -> dict[str, Any]:
    return {
        "request": _request(),
        "case_file": _case_file(),
        "evidence_ledger": EvidenceLedger(
            run_id="run_p8_tools",
            snapshot_id="snap_p8_tools",
            as_of=AS_OF,
            items=[],
            conflict_ids=[],
        ),
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


def test_research_task_expands_groups_and_whitelists_params(
    monkeypatch: Any,
) -> None:
    gateways: list[_RecordingGateway] = []

    def _factory(*args: object, **kwargs: object) -> _RecordingGateway:
        gateway = _RecordingGateway(*args, **kwargs)  # type: ignore[arg-type]
        gateways.append(gateway)
        return gateway

    monkeypatch.setattr(
        "aistock_agent.brain_v4.graph.builder.ToolGateway", _factory
    )

    graph = build_research_graph(tool_client=object(), analyst_model=None)
    final: dict[str, Any] = asyncio.run(
        graph.ainvoke(_initial_state(), config={"recursion_limit": 500})
    )

    assert gateways, "research_task 应通过 ToolGateway 取数"
    calls = [call for gateway in gateways for call in gateway.calls]
    assert calls, "market_trace 研究任务必须产生工具调用"

    tool_names = {tool for tool, _ in calls}
    # 只出现原子工具；组名（含 fundamental）绝不进入网关调用。
    assert tool_names <= _EXPECTED_ATOMS
    assert tool_names & _FORBIDDEN_GROUPS == set()
    assert _EXPECTED_ATOMS <= tool_names

    for tool, params in calls:
        spec = TOOL_MAP[tool]
        assert set(params) <= spec.allowed_params, (
            f"{tool} 收到白名单外参数：{sorted(set(params) - spec.allowed_params)}"
        )
        # 编排字段一律不得进入工具参数。
        assert set(params) & {
            "round_no",
            "subject_code",
            "tool",
            "hypothesis_ref",
            "tool_group",
        } == set()

    unexpected = [
        entry
        for entry in final.get("errors", ())
        if "unexpected params" in str(entry.get("detail", ""))
    ]
    assert unexpected == []
