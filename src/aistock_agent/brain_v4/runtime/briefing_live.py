"""早/午/晚报 live LLM 层：由分析师内核产出 claim，确定性投影只作降级。

为什么需要本模块：`runtime/runner.py::_run_briefing` 过去只调用
`features/briefing/projection.py::project_chain`，是纯确定性投影。即便注入模型，
早/午/晚报也从不产生 LLM claim，不满足设计文档附录 C.2「四试点 profile 的 claim
必须由分析师内核产出」。

本模块只做三件事：
1. 把某个时段的显式时间边界（profile / subject / as_of / data_cutoff /
   snapshot_id）映射成 `ResearchRequest`；
2. 以链上的竞争假设为起点，`build_research_graph` 跑一次真实研究图，把分析师
   `AnalystResult` 收敛成带 `claim_ref` 的 claim 列表；
3. 任何执行失败都收敛为 `status="degraded"`，绝不抛裸异常，交由 runner 回退
   到既有确定性投影并写 `degraded_reason`。

时间纪律：`as_of` / `data_cutoff` / `snapshot_id` 全部来自 `BriefingSessionSeed`，
本模块不读取系统时间。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig

from aistock_agent.brain_v4.analysts.kernel import AnalystResult
from aistock_agent.brain_v4.contracts.deliberation import CaseFile
from aistock_agent.brain_v4.contracts.evidence import EvidenceItem, EvidenceLedger
from aistock_agent.brain_v4.contracts.judgment import V4Judgment
from aistock_agent.brain_v4.contracts.research import Hypothesis, ResearchRequest
from aistock_agent.brain_v4.features.briefing.adapter import (
    BriefingChainFixture,
    BriefingSessionSeed,
    build_evidence_window,
    is_visible,
)
from aistock_agent.brain_v4.graph.builder import ResearchGraphState, build_research_graph
from aistock_agent.brain_v4.tools.gateway import ToolClient

#: 版本号与 runner 的 `_VERSION` 保持同一口径；集中在此便于审计追溯。
_CONFIG_VERSION = "brain-v4-briefing-live-1"
_MODEL_VERSION = "brain-v4-briefing-live-1"
_PROMPT_VERSION = "brain-v4-briefing-live-1"
_METHOD_VERSION = "brain-v4-briefing-live-1"

_EMPTY_USAGE: dict[str, object] = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "tool_calls": 0,
    "token_source": "unavailable",
}


@dataclass(frozen=True)
class BriefingSessionOutcome:
    """一个时段的研究图出口；失败时 `judgment is None` 且不抛裸异常。"""

    profile: str
    judgment: V4Judgment | None
    analyst_claims: list[dict[str, object]]
    usage: dict[str, object]
    status: str
    errors: list[str] = field(default_factory=list)


def build_briefing_request(
    chain: BriefingChainFixture,
    session: BriefingSessionSeed,
    *,
    event_ids: Sequence[str] | None = None,
) -> ResearchRequest:
    """把时段的显式边界映射成研究请求；时间全部取自 session。"""
    goal = "Which claims should this briefing commit to, based on visible evidence?"
    if event_ids:
        goal = f"{goal} focus_events={len(list(event_ids))}"
    return ResearchRequest(
        request_id=f"req_{session.run_id}",
        run_id=session.run_id,
        profile=session.profile,
        subject_type="market",
        subject_code=chain.subject_code,
        research_goal=goal,
        required_questions=["main_driver", "second_order_risk"],
        horizon="intraday",
        as_of=session.as_of,
        data_cutoff=session.data_cutoff,
        snapshot_id=session.snapshot_id,
        config_version=_CONFIG_VERSION,
        model_version=_MODEL_VERSION,
        prompt_version=_PROMPT_VERSION,
        method_version=_METHOD_VERSION,
        budget_policy=session.profile,
        stop_policy="default",
    )


def _briefing_hypotheses(chain: BriefingChainFixture) -> list[Hypothesis]:
    """把链上的竞争假设转成研究假设，`hypothesis_id` 复用 `claim_ref`。

    这样分析师结论与既有 claim 天然对齐，runner 覆盖叙述字段时无需再做映射。
    """
    return [
        Hypothesis(
            hypothesis_id=seed.claim_ref,
            version=1,
            statement=seed.statement,
            status="open",
            prior_confidence=seed.confidence,
            assumptions=[],
            disconfirmers=[f"若证据否定：{seed.statement}"],
            required_evidence_types=[],
            scope="market",
        )
        for seed in chain.hypotheses
    ]


def _case_file(
    chain: BriefingChainFixture,
    session: BriefingSessionSeed,
    request: ResearchRequest,
    hypotheses: Sequence[Hypothesis],
    items: Sequence[EvidenceItem],
) -> CaseFile:
    return CaseFile(
        case_id=f"case_{session.run_id}",
        run_id=request.run_id,
        profile=request.profile,
        case_version=1,
        question=request.research_goal,
        decision_type="briefing_claim",
        materiality="medium",
        subject="market",
        horizon=request.horizon,
        as_of=request.as_of,
        snapshot_id=request.snapshot_id,
        frozen_fact_ids=sorted(item.evidence_id for item in items),
        hypothesis_refs=sorted(hypothesis.ref for hypothesis in hypotheses),
        open_issue_ids=sorted(
            f"issue_{hypothesis.hypothesis_id}" for hypothesis in hypotheses
        ),
        deadline=request.as_of + timedelta(hours=1),
        budget_policy=request.budget_policy,
        decision_rubric_version="rubric_v1",
    )


def _source_briefs(
    chain: BriefingChainFixture, session: BriefingSessionSeed
) -> dict[str, str]:
    """按提供方聚合该时段可见证据的内容摘要，供分析师任务文本使用。"""
    grouped: dict[str, list[str]] = {}
    for seed in chain.items:
        if not is_visible(seed, session):
            continue
        content = seed.content.strip()
        if not content:
            continue
        bucket = grouped.setdefault(seed.source, [])
        if len(bucket) < 4:
            bucket.append(content)
    return {source: " | ".join(contents)[:600] for source, contents in grouped.items()}


def _initial_state(
    chain: BriefingChainFixture,
    session: BriefingSessionSeed,
    request: ResearchRequest,
    hypotheses: Sequence[Hypothesis],
) -> dict[str, Any]:
    """构造研究图初始状态：只给显式边界与已可见证据，不读系统时间。"""
    window = build_evidence_window(chain, session.profile)
    items = sorted(window.items, key=lambda item: item.evidence_id)
    ledger = EvidenceLedger(
        run_id=request.run_id,
        snapshot_id=request.snapshot_id,
        as_of=request.as_of,
        items=items,
        conflict_ids=[],
    )
    return {
        "request": request,
        "case_file": _case_file(chain, session, request, hypotheses, items),
        "evidence_ledger": ledger,
        # 分析师要看到“证据是什么”。简报证据的 `source` 是提供方标签（如
        # node_api:close-snapshot），这里把同提供方的种子内容拼成一段摘要透传。
        "evidence_sources": _source_briefs(chain, session),
        "hypotheses": list(hypotheses),
        "causal_graph": [],
        "research_gaps": [],
        "round_ledgers": [],
        "publish_ready": False,
        "counter_evidence_coverage": 1.0,
        "parent_judgment_ids": [],
        "as_of": session.as_of,
        "data_cutoff": session.data_cutoff,
        "snapshot_id": session.snapshot_id,
        "usage": [],
        "errors": [],
        "trace": [],
    }


def _aggregate_usage(records: Sequence[object]) -> dict[str, object]:
    """从图状态 usage 聚合 token；模型未回报时如实标 `unavailable`。"""
    totals: dict[str, float] = {}
    for record in records:
        if not isinstance(record, Mapping):
            continue
        key = record.get("key")
        value = record.get("value")
        if (
            isinstance(key, str)
            and isinstance(value, int | float)
            and not isinstance(value, bool)
        ):
            totals[key] = totals.get(key, 0.0) + float(value)

    analyst_tokens = int(totals.get("analyst_tokens", 0))
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": analyst_tokens,
        "tool_calls": int(totals.get("tool_calls", 0)),
        "token_source": "graph_analyst_tokens" if analyst_tokens > 0 else "unavailable",
    }


def _attributed_claim_ref(
    result: AnalystResult,
    ref_to_claim: Mapping[str, str],
    evidence_by_id: Mapping[str, EvidenceItem],
) -> str | None:
    """按分析师引用的证据反查其支撑的 claim_ref；无法溯源时返回 None。"""
    refs: set[str] = set()
    for evidence_id in result.evidence_refs:
        item = evidence_by_id.get(evidence_id)
        if item is None:
            continue
        refs.update(item.supports_hypothesis_refs)
    candidates = sorted(ref_to_claim[ref] for ref in refs if ref in ref_to_claim)
    return candidates[0] if candidates else None


def _claims_from_results(
    chain: BriefingChainFixture,
    results: Sequence[AnalystResult],
    ledger: EvidenceLedger | None,
) -> list[dict[str, object]]:
    """把分析师结构化结果收敛成带 claim_ref 的 claim 列表。

    归属规则：优先用「分析师引用证据 → 证据支撑的假设 → claim_ref」溯源；
    无证据可溯源时按稳定顺序回落到链上 claim（不新增 claim、不虚构证据）。
    """
    claim_refs = [seed.claim_ref for seed in chain.hypotheses]
    if not claim_refs:
        return []
    ref_to_claim = {
        f"{seed.claim_ref}:v1": seed.claim_ref for seed in chain.hypotheses
    }
    evidence_by_id = (
        {item.evidence_id: item for item in ledger.items}
        if ledger is not None
        else {}
    )
    ordered = sorted(results, key=lambda item: (item.research_round, item.result_id))
    claims: list[dict[str, object]] = []
    for index, result in enumerate(ordered):
        # 只要分析师给出了非空 claim 就纳入：stalled/error 往往发生在工具步数
        # 耗尽之后，claim 仍可能是有效结论。之前按 status=="ok" 过滤，导致
        # 真实 run 花掉 1.7 万 token 却一条 claim 都没进早报（llm_claim_count=0）。
        if not result.claim.strip():
            continue
        claim_ref = _attributed_claim_ref(result, ref_to_claim, evidence_by_id)
        if claim_ref is None:
            claim_ref = claim_refs[index % len(claim_refs)]
        claims.append(
            {
                "claim_ref": claim_ref,
                "statement": result.claim,
                "confidence": float(result.confidence),
                "evidence_ids": list(result.evidence_refs),
                # 审计需要知道这条 claim 来自正常完成还是降级结果。
                "analyst_status": result.status,
                "analyst_error_kind": result.error_kind,
            }
        )
    return claims


def _error_text(error: Mapping[str, Any]) -> str:
    kind = error.get("error_kind") or error.get("error_id") or "unknown"
    detail = error.get("detail")
    return f"{kind}: {detail}" if detail else str(kind)


async def run_briefing_session(
    chain: BriefingChainFixture,
    session: BriefingSessionSeed,
    *,
    model: BaseChatModel,
    tool_client: ToolClient,
) -> BriefingSessionOutcome:
    """为一个时段跑一次真实研究图；任何失败都收敛为 degraded，不抛裸异常。"""
    profile = session.profile
    hypotheses = _briefing_hypotheses(chain)
    try:
        request = build_briefing_request(chain, session)
        initial = _initial_state(chain, session, request, hypotheses)
        # 工具循环可能因步数上限提前结束、拿不到结构化 claim；此时用一次
        # 不带工具的 thinking 综合兜底产出结论（实测早报因此从 0 条 claim 修复）。
        # 测试环境会把 `_build_thinking_model_safe` 替换成 None，保持全离线。
        from aistock_agent.brain_v4.runtime.graph_run import (
            _build_thinking_model_safe,
        )

        graph = build_research_graph(
            analyst_model=model,
            tool_client=tool_client,
            thinking_model=_build_thinking_model_safe(),
        )
        final = cast(
            Mapping[str, Any],
            await graph.ainvoke(
                cast(ResearchGraphState, initial),
                config=cast(RunnableConfig, {"recursion_limit": 1000}),
            ),
        )
    except Exception as exc:  # noqa: BLE001 - 图失败必须降级而不是抛裸异常
        return BriefingSessionOutcome(
            profile=profile,
            judgment=None,
            analyst_claims=[],
            usage=dict(_EMPTY_USAGE),
            status="degraded",
            errors=[f"{type(exc).__name__}: {exc}"],
        )

    raw_judgment = final.get("judgment_draft")
    judgment = raw_judgment if isinstance(raw_judgment, V4Judgment) else None
    raw_ledger = final.get("evidence_ledger")
    ledger = raw_ledger if isinstance(raw_ledger, EvidenceLedger) else None
    results = [
        item
        for item in final.get("analyst_results", ())
        if isinstance(item, AnalystResult)
    ]
    analyst_claims = _claims_from_results(chain, results, ledger)
    usage = _aggregate_usage(list(final.get("usage", ())))
    errors = [
        _error_text(dict(item))
        for item in final.get("errors", ())
        if isinstance(item, Mapping)
    ]
    # 分析师降级（stalled / error）必须显式进审计，否则只看到 token 花掉、
    # 不知道结论为何薄弱。
    for result in results:
        if result.status != "ok":
            errors.append(
                f"analyst_{result.status}:{result.error_kind or 'unknown'}"
            )
    if judgment is None and not errors:
        errors = ["judgment_missing"]
    status = "ok" if judgment is not None else "degraded"
    return BriefingSessionOutcome(
        profile=profile,
        judgment=judgment,
        analyst_claims=analyst_claims,
        usage=usage,
        status=status,
        errors=errors,
    )


__all__ = [
    "BriefingSessionOutcome",
    "build_briefing_request",
    "run_briefing_session",
]
