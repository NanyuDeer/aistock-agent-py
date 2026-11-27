"""V4 实时运行器：真实取数 → V4 主图/时段链 → 统一结果 + 前端投影。

为什么需要独立运行器而不是直接调 graph：本包要求同一天同 profile 幂等、
早/午/晚 parent 关系可核验、取数失败可降级，并且要产出一份 P4 可直接归档的
`payload`。把这些编排职责收在一处，features/ 的既有 projection 语义无需改动。

时间纪律：`date` / `as_of` 全部显式入参，本模块不读取系统时间；同输入必然
得到同一 snapshot_id / SHA。
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import date as date_type
from datetime import datetime, time, timedelta, timezone
from typing import Literal, cast

from langchain_core.language_models.chat_models import BaseChatModel
from pydantic import BaseModel, ConfigDict, Field

from aistock_agent.brain_v4.contracts import V4Contract
from aistock_agent.brain_v4.contracts.briefing import BriefingProfile
from aistock_agent.brain_v4.contracts.evidence import EvidenceLedger
from aistock_agent.brain_v4.contracts.judgment import V4Judgment
from aistock_agent.brain_v4.contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchGap,
    RevisionEvent,
)
from aistock_agent.brain_v4.features.briefing.adapter import (
    BriefingChainFixture,
    BriefingSessionSeed,
    MarketTraceReference,
    build_evidence_window,
    compute_window_sha,
)
from aistock_agent.brain_v4.features.briefing.projection import (
    BriefingChainResult,
    BriefingClaim,
    BriefingResult,
    briefing_judgment_id,
    project_chain,
)
from aistock_agent.brain_v4.features.market_trace.adapter import (
    MarketTraceCase,
    compute_snapshot_sha,
)
from aistock_agent.brain_v4.features.market_trace.profile import (
    ResearchRun,
    run_market_trace,
)
from aistock_agent.brain_v4.features.market_trace.projection import (
    SixFactorChain,
    project_six_factor,
)
from aistock_agent.brain_v4.runtime.bootstrap import (
    ensure_app_env,
    ensure_runtime_pools,
)
from aistock_agent.brain_v4.runtime.briefing_live import run_briefing_session
from aistock_agent.brain_v4.runtime.briefing_projection import (
    ProjectionSession,
    to_briefing_v1,
)
from aistock_agent.brain_v4.runtime.graph_run import (
    LiveMarketToolClient,
    ResearchOutcome,
    run_research_graph,
)
from aistock_agent.brain_v4.runtime.inputs import (
    BRIEFING_PROFILES,
    EVENING_AS_OF,
    EVENING_CUTOFF,
    MARKET_TRACE_AS_OF,
    MARKET_TRACE_CUTOFF,
    MIDDAY_AS_OF,
    MIDDAY_CUTOFF,
    MORNING_AS_OF,
    MORNING_CUTOFF,
    NodeDataSource,
    build_briefing_chain,
    build_market_trace_case,
)
from aistock_agent.brain_v4.tools.gateway import ToolGateway

CST = timezone(timedelta(hours=8))
LiveProfile = Literal["market_trace", "briefing_morning", "briefing_midday", "briefing_evening"]
LiveStatus = Literal["ok", "partial", "insufficient"]

_VERSION = "brain-v4-live-1"

#: 只有日历/公告类证据时不允许调用分析师：没有行情事实时，任何归因都是编造。
_MARKET_SOURCE_TOKENS = ("close-snapshot", "last-close-snapshot", "intraday")


def _has_market_evidence(items: Sequence[object]) -> bool:
    """窗口内是否含真实行情证据（收盘/上一收盘/盘中）。"""
    for item in items:
        source = getattr(item, "source", "")
        if isinstance(source, str) and any(
            token in source for token in _MARKET_SOURCE_TOKENS
        ):
            return True
    return False


def _llm_evidence_ok(items: Sequence[object]) -> bool:
    """证据是否足以支撑一次真实研判：必须含行情事实。

    实测故障：早报取数失败时窗口里只剩 2 条日历事件，分析师仍然被调用，
    花掉 1.5 万 token 却写不出任何 claim。没有行情事实时任何归因都是编造，
    因此这里只认行情证据，不按条数放行。
    """
    return _has_market_evidence(items)

ArchiveFn = Callable[[dict[str, object]], Awaitable[None] | None]


class LiveRunResult(BaseModel):
    """一次实时运行的全部出口；`payload` 是 P4 归档的完整 JSON 结构。"""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    profile: str
    as_of: datetime
    data_cutoff: datetime
    snapshot_id: str
    snapshot_sha: str
    status: LiveStatus
    judgment: V4Judgment | None = None
    briefing_v1: dict[str, object] | None = None
    six_factor: SixFactorChain | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    audit: dict[str, object] = Field(default_factory=dict)
    payload: dict[str, object] = Field(default_factory=dict)
    parent_run_id: str | None = None
    parent_judgment_ids: list[str] = Field(default_factory=list)


#: 进程内幂等注册表：同日同 profile 直接复用，避免重复取数。
#: 跨进程/跨天幂等由 P4 归档层负责，这里不假装已经持久化。
_RUNS: dict[tuple[date_type, str], LiveRunResult] = {}


def reset_live_runs() -> None:
    """清空进程内注册表；测试与手动重跑使用，生产进程按天自然隔离。"""
    _RUNS.clear()


def _at(day: date_type, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=CST)


def _window_for(profile: str, day: date_type) -> tuple[datetime, datetime]:
    if profile == "market_trace":
        return _at(day, MARKET_TRACE_AS_OF), _at(day, MARKET_TRACE_CUTOFF)
    if profile == "briefing_morning":
        return _at(day, MORNING_AS_OF), _at(day, MORNING_CUTOFF)
    if profile == "briefing_midday":
        return _at(day, MIDDAY_AS_OF), _at(day, MIDDAY_CUTOFF)
    if profile == "briefing_evening":
        return _at(day, EVENING_AS_OF), _at(day, EVENING_CUTOFF)
    raise ValueError(f"unknown V4 live profile: {profile!r}")


def _ensure_client(client: NodeDataSource | None) -> NodeDataSource:
    if client is not None:
        return client
    # 惰性导入：data_client 会读 settings，测试传入假客户端时完全不需要加载。
    from aistock_agent.services.data_client import NodeApiClient

    return cast(NodeDataSource, NodeApiClient())


def _resolve_model(
    profile: str, model: BaseChatModel | None, use_llm: bool
) -> tuple[BaseChatModel | None, str | None]:
    """按 profile 构造分析师模型；失败只记录降级原因，不阻断运行。"""
    if model is not None or not use_llm:
        return model, None
    try:
        # 惰性导入：构造 ChatOpenAI 会读 settings；只有 use_llm 且未注入模型时才加载。
        from aistock_agent.brain_v4.analysts.model_factory import build_analyst_model
        from aistock_agent.brain_v4.graph.profiles import get_profile

        return build_analyst_model(get_profile(profile)), None
    except Exception as exc:  # noqa: BLE001 - 模型不可用时降级为确定性分析
        return None, f"model_unavailable:{type(exc).__name__}: {exc}"


def _usage_from_outcome(
    source: dict[str, object], *, evidence_count: int
) -> dict[str, int]:
    """把研究图 usage 收敛成 `LiveRunResult.usage` 的整数字段，不虚报。"""
    def _value(key: str) -> int:
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value

    return {
        "prompt_tokens": _value("prompt_tokens"),
        "completion_tokens": _value("completion_tokens"),
        "total_tokens": _value("total_tokens"),
        "tool_calls": _value("tool_calls"),
        "evidence_count": evidence_count,
    }


async def run_live(
    profile: str,
    date: date_type,
    *,
    client: NodeDataSource | None = None,
    model: BaseChatModel | None = None,
    gateway: ToolGateway | None = None,
    archiver: ArchiveFn | None = None,
    use_llm: bool = True,
) -> LiveRunResult:
    """按 profile + date 运行 V4，返回统一结果；同日同 profile 幂等。

    独立进程没有 FastAPI lifespan：进入前先确保 `APP_ENV` 与连接池就绪，
    否则 `NodeApiClient` 的取数会被统一降级成 None。`use_llm` 为真且未注入
    模型时尝试构造真实分析师模型；market_trace 会真正执行 LLM 研究图。
    """
    key = (date, profile)
    cached = _RUNS.get(key)
    if cached is not None:
        return cached

    # 必须在构造 NodeApiClient / Settings 之前设置 APP_ENV，并幂等初始化连接池。
    ensure_app_env()
    await ensure_runtime_pools()

    resolved_model, model_degraded = _resolve_model(profile, model, use_llm)
    data_source = _ensure_client(client)
    if profile == "market_trace":
        result = await _run_market_trace(
            date,
            data_source,
            model=resolved_model,
            gateway=gateway,
            model_degraded=model_degraded,
        )
    elif profile in BRIEFING_PROFILES:
        result = await _run_briefing(
            cast(BriefingProfile, profile),
            date,
            data_source,
            model=resolved_model,
            gateway=gateway,
            model_degraded=model_degraded,
        )
    else:
        raise ValueError(f"unknown V4 live profile: {profile!r}")

    if archiver is not None:
        result = await _archive(result, archiver)
    _RUNS[key] = result
    return result


async def _run_market_trace(
    day: date_type,
    client: NodeDataSource,
    *,
    model: BaseChatModel | None,
    gateway: ToolGateway | None,
    model_degraded: str | None = None,
) -> LiveRunResult:
    as_of, data_cutoff = _window_for("market_trace", day)
    snapshot_id = f"snap_market_trace_{day.strftime('%Y%m%d')}"
    inputs = await build_market_trace_case(client, as_of, data_cutoff, snapshot_id)
    case = inputs.case
    snapshot_sha = compute_snapshot_sha(case.snapshot)

    judgment: V4Judgment | None
    hypotheses: list[Hypothesis]
    ledger: EvidenceLedger | None
    gaps: list[ResearchGap]
    revisions: list[RevisionEvent]
    causal_edges: list[CausalEdge]
    collaboration_mode: str
    sufficiency_passed: bool
    six_factor: SixFactorChain
    token_source: str
    research_graph_failed: str | None = None
    degraded_reason: str | None = None

    outcome: ResearchOutcome | None
    try:
        tool_client = LiveMarketToolClient(
            client,
            as_of=as_of,
            data_cutoff=data_cutoff,
            subject_code=case.subject_code,
        )
        outcome = await run_research_graph(
            case, model=model, tool_client=tool_client, gateway=gateway
        )
    except Exception as exc:  # noqa: BLE001 - 图执行失败才回退确定性脚本
        outcome = None
        research_graph_failed = f"{type(exc).__name__}: {exc}"

    if outcome is not None and outcome.status == "ok" and outcome.judgment is not None:
        judgment = outcome.judgment
        hypotheses = list(outcome.hypotheses)
        ledger = outcome.evidence_ledger
        gaps = list(outcome.gaps)
        revisions = list(outcome.revision_events)
        causal_edges = list(outcome.causal_edges)
        collaboration_mode = outcome.collaboration_mode
        sufficiency_passed = outcome.sufficiency_passed
        token_source = str(outcome.usage.get("token_source") or "unavailable")
        evidence_ids = (
            sorted(item.evidence_id for item in ledger.items)
            if ledger is not None
            else []
        )
        six_factor = project_six_factor(
            hypotheses=hypotheses,
            causal_edges=causal_edges,
            evidence_ids=set(evidence_ids),
            snapshot_id=case.snapshot.snapshot_id,
            gate_passed=sufficiency_passed,
        )
        usage = _usage_from_outcome(
            outcome.usage, evidence_count=len(evidence_ids)
        )
    else:
        if research_graph_failed is None and outcome is not None:
            kinds = sorted(
                str(error.get("error_kind") or error.get("error_id"))
                for error in outcome.errors
            )
            research_graph_failed = "no_judgment" + (
                f":{','.join(kinds)}" if kinds else ""
            )
        # 研究图失败才回退写死的确定性脚本，并在审计显式标记。
        try:
            run = run_market_trace(case)
            judgment = run.judgment
            hypotheses = list(run.hypotheses)
            ledger = run.evidence_ledger
            gaps = list(run.research_gaps)
            revisions = list(run.revision_events)
            collaboration_mode = run.collaboration_decision.mode
            sufficiency_passed = run.sufficiency_passed
            token_source = "unavailable"
            evidence_ids = sorted(
                item.evidence_id for item in run.evidence_ledger.items
            )
            six_factor = _six_factor(run)
            usage = _usage(
                tool_calls=inputs.tool_calls, evidence_count=len(evidence_ids)
            )
        except Exception as exc:  # noqa: BLE001 - 兜底降级，不抛裸异常
            judgment = _degraded_judgment(
                run_id=case.run_id,
                profile="market_trace",
                subject_code=case.subject_code,
                as_of=as_of,
                data_cutoff=data_cutoff,
                snapshot_id=snapshot_id,
                parent_ids=[],
                reason=f"runner_error:{type(exc).__name__}",
            )
            hypotheses = []
            ledger = None
            gaps = []
            revisions = []
            collaboration_mode = "DEGRADED"
            sufficiency_passed = False
            token_source = "unavailable"
            evidence_ids = []
            six_factor = _empty_six_factor(case.snapshot.snapshot_id)
            usage = _usage(
                tool_calls=inputs.tool_calls, evidence_count=0
            )
            degraded_reason = f"{type(exc).__name__}: {exc}"

    status: LiveStatus = judgment.status
    audit = _audit(
        mode=collaboration_mode,
        evidence_count=len(evidence_ids),
        gap_count=len(gaps),
        revision_count=len(revisions),
        missing_fields=inputs.missing_fields,
        time_leakage_count=inputs.time_leakage_count,
        insufficient_reasons=(
            [] if status == "ok" else [judgment.stop_reason or "judgment_insufficient"]
        ),
        legacy_output_present=inputs.legacy_output is not None,
        model_injected=model is not None,
        gateway_injected=gateway is not None,
        snapshot_sha=snapshot_sha,
    )
    audit["token_source"] = token_source
    audit["usage_source"] = (
        "deterministic_fallback"
        if research_graph_failed is not None
        else "research_graph"
    )
    if model_degraded is not None:
        audit["degraded_reason"] = model_degraded
    if degraded_reason is not None:
        audit["degraded_reason"] = degraded_reason
    if research_graph_failed is not None:
        audit["research_graph_failed"] = research_graph_failed

    payload = _market_trace_payload(
        case=case,
        judgment=judgment,
        six_factor=six_factor,
        status=status,
        snapshot_sha=snapshot_sha,
        usage=usage,
        audit=audit,
        missing_fields=inputs.missing_fields,
        legacy_output=inputs.legacy_output,
        evidence_ids=evidence_ids,
        evidence_ledger=ledger,
        hypotheses=hypotheses,
        gaps=gaps,
        revisions=revisions,
        collaboration_mode=collaboration_mode,
    )
    return LiveRunResult(
        run_id=case.run_id,
        profile="market_trace",
        as_of=as_of,
        data_cutoff=data_cutoff,
        snapshot_id=snapshot_id,
        snapshot_sha=snapshot_sha,
        status=status,
        judgment=judgment,
        briefing_v1=None,
        six_factor=six_factor,
        usage=usage,
        audit=audit,
        payload=payload,
        parent_run_id=None,
        parent_judgment_ids=[],
    )


def _six_factor(run: ResearchRun) -> SixFactorChain:
    return project_six_factor(
        hypotheses=run.hypotheses,
        causal_edges=run.causal_graph,
        evidence_ids={item.evidence_id for item in run.evidence_ledger.items},
        snapshot_id=run.snapshot_id,
        gate_passed=run.sufficiency_passed,
    )


def _empty_six_factor(snapshot_id: str) -> SixFactorChain:
    return project_six_factor(
        hypotheses=[],
        causal_edges=[],
        evidence_ids=set(),
        snapshot_id=snapshot_id,
        gate_passed=False,
    )


def _market_trace_payload(
    *,
    case: MarketTraceCase,
    judgment: V4Judgment | None,
    six_factor: SixFactorChain,
    status: LiveStatus,
    snapshot_sha: str,
    usage: dict[str, int],
    audit: dict[str, object],
    missing_fields: list[str],
    legacy_output: dict[str, object] | None,
    evidence_ids: list[str],
    evidence_ledger: EvidenceLedger | None,
    hypotheses: list[Hypothesis],
    gaps: list[ResearchGap],
    revisions: list[RevisionEvent],
    collaboration_mode: str,
) -> dict[str, object]:
    return {
        "run_id": case.run_id,
        "profile": "market_trace",
        "as_of": case.as_of.isoformat(),
        "data_cutoff": case.data_cutoff.isoformat(),
        "snapshot_id": case.snapshot.snapshot_id,
        "snapshot_sha": snapshot_sha,
        "status": status,
        "judgment": _dump(judgment),
        "briefing_v1": None,
        "six_factor": six_factor.model_dump(mode="json"),
        "evidence_ledger": _dump(evidence_ledger),
        "hypotheses": [hypothesis.model_dump(mode="json") for hypothesis in hypotheses],
        "gaps": [gap.model_dump(mode="json") for gap in gaps],
        "revisions": [event.model_dump(mode="json") for event in revisions],
        "collaboration_mode": collaboration_mode,
        "usage": usage,
        "audit": audit,
        "parent_run_id": None,
        "parent_judgment_ids": [],
        "legacy_output": legacy_output,
        "missing_fields": missing_fields,
        "evidence_ids": evidence_ids,
        "evidence": [
            item.model_dump(mode="json")
            for item in (evidence_ledger.items if evidence_ledger is not None else [])
        ],
        # 证据标题在 evidence 投影里被压缩掉；来源表保留它供审计/前端核对。
        "sources": [
            source.model_dump(mode="json")
            for source in case.snapshot.sources.values()
        ],
    }


async def _run_briefing(
    profile: BriefingProfile,
    day: date_type,
    client: NodeDataSource,
    *,
    model: BaseChatModel | None,
    gateway: ToolGateway | None,
    model_degraded: str | None = None,
) -> LiveRunResult:
    as_of, data_cutoff = _window_for(profile, day)
    inputs = await build_briefing_chain(client, day, profile)
    chain = inputs.chain

    # 晚报必须按 ID 引用当日市场溯源研判；若尚未运行则先补跑（幂等）。
    # 模型已在上层解析，递归补跑禁止再次尝试建模型。
    market_trace = await run_live(
        "market_trace",
        day,
        client=client,
        model=model,
        gateway=gateway,
        use_llm=False,
    )
    reference = _market_trace_reference(market_trace)
    parent_run_id, parent_judgment_ids = _expected_parents(chain, profile)

    # 分析师内核产出的 claim 是否可用；缺模型/图失败都回退确定性投影。
    token_source = "unavailable"
    llm_claim_count = 0
    llm_degraded: str | None = None
    usage_override: dict[str, int] | None = None
    degraded_reason: str | None = None

    try:
        chain_result = project_chain(chain, reference)
        result = _pick_result(chain_result, profile)
        window = build_evidence_window(chain, profile)
        status = _briefing_status(bool(window.items), inputs.missing_fields)

        if model is not None and _llm_evidence_ok(window.items):
            # 注入模型时，由分析师内核产出 claim；成功则覆盖叙述字段，
            # 失败/无研判则保留确定性投影并显式记录降级原因。
            session = _session_for(chain, profile)
            tool_client = LiveMarketToolClient(
                client,
                as_of=as_of,
                data_cutoff=data_cutoff,
                subject_code=chain.subject_code,
            )
            outcome = await run_briefing_session(
                chain, session, model=model, tool_client=tool_client
            )
            token_source = str(outcome.usage.get("token_source") or "unavailable")
            usage_override = _session_usage(
                outcome.usage,
                tool_calls=inputs.tool_calls,
                evidence_count=len(window.items),
            )
            if outcome.judgment is not None:
                llm_claim_count = _apply_analyst_claims(
                    result, outcome.analyst_claims
                )
            else:
                reason = (
                    "; ".join(outcome.errors) if outcome.errors else "no_judgment"
                )
                llm_degraded = f"briefing_graph_failed:{reason}"
                if status == "ok":
                    status = "partial"
        else:
            llm_degraded = (
                f"briefing_llm_unavailable:{model_degraded or 'model_not_injected'}"
                if model is None
                else f"briefing_evidence_too_thin:{len(window.items)}"
            )

        judgment = _briefing_judgment(
            result=result,
            chain=chain,
            window_snapshot_id=window.snapshot_id,
            status=status,
        )
        briefing_v1 = to_briefing_v1(
            result,
            ProjectionSession(
                profile=profile,
                trading_date=day,
                evening_extras=inputs.evening_extras,
            ),
        )
        # 投影可能拿不到 summary（种子缺摘要），导致前端看到空标题。此时用
        # 分析师的首条 claim 回填，保证"花了 token"能变成可读结论。
        _fill_summary_from_claims(briefing_v1, result)
    except Exception as exc:  # noqa: BLE001 - 数据缺口降级，不抛裸异常
        status = "insufficient"
        judgment = _degraded_judgment(
            run_id=f"v4_{day.isoformat()}_{profile}_1",
            profile=profile,
            subject_code=chain.subject_code,
            as_of=as_of,
            data_cutoff=data_cutoff,
            snapshot_id=f"snap_{profile}_{day.strftime('%Y%m%d')}",
            parent_ids=parent_judgment_ids,
            reason=f"briefing_projection_error:{type(exc).__name__}",
        )
        briefing_v1 = _degraded_briefing_v1(profile, day, as_of)
        result = None
        window = None
        degraded_reason = f"{type(exc).__name__}: {exc}"
        usage_override = None

    snapshot_id = (
        window.snapshot_id
        if window is not None
        else f"snap_{profile}_{day.strftime('%Y%m%d')}"
    )
    snapshot_sha = compute_window_sha(window) if window is not None else ""
    evidence_ids = (
        [item.evidence_id for item in window.items] if window is not None else []
    )
    usage = (
        usage_override
        if usage_override is not None
        else _usage(tool_calls=inputs.tool_calls, evidence_count=len(evidence_ids))
    )
    audit = _audit(
        mode="BRIEFING_CHAIN",
        evidence_count=len(evidence_ids),
        gap_count=0,
        revision_count=len(result.changes) if result is not None else 0,
        missing_fields=inputs.missing_fields,
        time_leakage_count=inputs.time_leakage_count,
        insufficient_reasons=(
            [] if status == "ok" else [_briefing_stop_reason(status)]
        ),
        legacy_output_present=any(
            value is not None for value in inputs.legacy_outputs.values()
        ),
        model_injected=model is not None,
        gateway_injected=gateway is not None,
        snapshot_sha=snapshot_sha,
    )
    audit["llm_claim_count"] = llm_claim_count
    audit["token_source"] = token_source
    if degraded_reason is not None:
        audit["degraded_reason"] = degraded_reason
    elif llm_degraded is not None:
        audit["degraded_reason"] = llm_degraded
    elif model_degraded is not None:
        audit["degraded_reason"] = model_degraded

    payload = _briefing_payload(
        profile=profile,
        chain=chain,
        result=result,
        judgment=judgment,
        briefing_v1=briefing_v1,
        snapshot_id=snapshot_id,
        snapshot_sha=snapshot_sha,
        status=status,
        usage=usage,
        audit=audit,
        missing_fields=inputs.missing_fields,
        legacy_outputs=inputs.legacy_outputs,
        evidence_ids=evidence_ids,
        window_items=(
            [item.model_dump(mode="json") for item in window.items]
            if window is not None
            else []
        ),
        parent_run_id=parent_run_id,
        parent_judgment_ids=parent_judgment_ids,
    )
    return LiveRunResult(
        run_id=judgment.run_id,
        profile=profile,
        as_of=as_of,
        data_cutoff=data_cutoff,
        snapshot_id=snapshot_id,
        snapshot_sha=snapshot_sha,
        status=status,
        judgment=judgment,
        briefing_v1=briefing_v1,
        six_factor=None,
        usage=usage,
        audit=audit,
        payload=payload,
        parent_run_id=parent_run_id,
        parent_judgment_ids=parent_judgment_ids,
    )


def _market_trace_reference(market_trace: LiveRunResult) -> MarketTraceReference:
    if market_trace.judgment is None:
        raise ValueError("market_trace run 缺少 judgment，无法供晚报引用")
    return MarketTraceReference(
        run_id=market_trace.run_id,
        judgment_id=market_trace.judgment.judgment_id,
        snapshot_id=market_trace.snapshot_id,
        decision=market_trace.judgment.decision,
        status=market_trace.judgment.status,
        evidence_ids=sorted(market_trace.judgment.evidence_ids),
        selected_claim_refs=list(market_trace.judgment.selected_hypothesis_refs),
    )


def _pick_result(
    chain_result: BriefingChainResult, profile: BriefingProfile
) -> BriefingResult:
    if profile == "briefing_morning":
        return chain_result.morning
    if profile == "briefing_midday":
        return chain_result.midday
    return chain_result.evening


def _expected_parents(
    chain: BriefingChainFixture, profile: BriefingProfile
) -> tuple[str | None, list[str]]:
    by_profile = {session.profile: session for session in chain.sessions}
    morning = by_profile["briefing_morning"]
    midday = by_profile["briefing_midday"]
    if profile == "briefing_morning":
        return None, []
    if profile == "briefing_midday":
        return morning.run_id, [briefing_judgment_id(morning.run_id)]
    return midday.run_id, [
        briefing_judgment_id(morning.run_id),
        briefing_judgment_id(midday.run_id),
    ]


def _session_for(
    chain: BriefingChainFixture, profile: BriefingProfile
) -> BriefingSessionSeed:
    for session in chain.sessions:
        if session.profile == profile:
            return session
    raise KeyError(f"briefing chain 缺少时段 {profile!r}")


def _session_usage(
    source: dict[str, object], *, tool_calls: int, evidence_count: int
) -> dict[str, int]:
    """把时段研究图 usage 收敛成 `LiveRunResult.usage`，不虚报 token。"""

    def _value(key: str) -> int:
        value = source.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return 0
        return value

    return {
        "prompt_tokens": _value("prompt_tokens"),
        "completion_tokens": _value("completion_tokens"),
        "total_tokens": _value("total_tokens"),
        "tool_calls": tool_calls,
        "evidence_count": evidence_count,
    }


def _apply_analyst_claims(
    result: BriefingResult, analyst_claims: list[dict[str, object]]
) -> int:
    """用分析师 claim 覆盖既有 claim 的 statement/confidence，返回覆盖条数。

    只改叙述字段，保留 `claim_ref`、`changes`、`scores` 与 envelope 结构；
    未匹配或文本为空的 claim 保持确定性投影原样，绝不新增 claim。
    """
    by_ref: dict[str, dict[str, object]] = {}
    for analyst_claim in analyst_claims:
        ref = analyst_claim.get("claim_ref")
        if isinstance(ref, str):
            by_ref[ref] = analyst_claim
    if not by_ref:
        return 0
    if not result.claims:
        # 确定性投影对某些日期/档位可能产出 0 条 claim；此时分析师结论无处
        # 落地，会出现"花了 token 但早报没有结论"。按附录 C.2，claim 应由分析师
        # 内核产出，因此这里用分析师结果补建 claim（不额外编造）。
        rebuilt: list[BriefingClaim] = []
        for item in analyst_claims:
            ref = item.get("claim_ref")
            statement = item.get("statement")
            if not isinstance(ref, str) or not isinstance(statement, str):
                continue
            if not statement.strip():
                continue
            raw = item.get("confidence")
            confidence = (
                float(raw)
                if isinstance(raw, int | float) and not isinstance(raw, bool)
                else 0.5
            )
            rebuilt.append(
                BriefingClaim(
                    claim_ref=ref,
                    source_profile=result.envelope.profile,
                    statement=statement,
                    confidence=min(1.0, max(0.0, confidence)),
                )
            )
        if rebuilt:
            result.claims = rebuilt
            return len(rebuilt)
        return 0

    updated: list[BriefingClaim] = []
    count = 0
    for claim in result.claims:
        override = by_ref.get(claim.claim_ref)
        statement = None if override is None else override.get("statement")
        if override is None or not isinstance(statement, str) or not statement.strip():
            updated.append(claim)
            continue
        confidence = claim.confidence
        raw_confidence = override.get("confidence")
        if isinstance(raw_confidence, int | float) and not isinstance(
            raw_confidence, bool
        ):
            confidence = min(1.0, max(0.0, float(raw_confidence)))
        updated.append(
            claim.model_copy(
                update={"statement": statement, "confidence": confidence}
            )
        )
        count += 1
    result.claims = updated
    return count


def _fill_summary_from_claims(
    briefing_v1: dict[str, object], result: BriefingResult
) -> None:
    """投影缺 summary 时用首条 claim 回填，避免前端出现空结论。"""
    display = briefing_v1.get("display_report")
    if not isinstance(display, dict):
        return
    current = display.get("summary")
    if isinstance(current, str) and current.strip():
        return
    for claim in result.claims:
        if claim.statement.strip():
            display["summary"] = claim.statement
            if not briefing_v1.get("podcast_brief"):
                briefing_v1["podcast_brief"] = claim.statement
            return


def _briefing_status(has_evidence: bool, missing_fields: list[str]) -> LiveStatus:
    if not has_evidence:
        return "insufficient"
    # legacy 仅是评测对照；market_data 缺失才降级。
    core_missing = [field for field in missing_fields if field != "legacy_review"]
    if core_missing:
        return "partial"
    return "ok"


def _briefing_stop_reason(status: LiveStatus) -> str:
    return "briefing_chain_complete" if status == "ok" else "briefing_data_degraded"


def _briefing_judgment(
    *,
    result: BriefingResult,
    chain: BriefingChainFixture,
    window_snapshot_id: str,
    status: LiveStatus,
) -> V4Judgment:
    envelope = result.envelope
    selected = [claim.claim_ref for claim in result.claims]
    if not selected:
        selected = [score.claim_ref for score in result.scores]
    decision: Literal["accept", "revise", "insufficient", "escalate"] = (
        "accept" if status == "ok" else "insufficient"
    )
    return V4Judgment(
        judgment_id=result.judgment_id,
        run_id=envelope.run_id,
        profile=result.envelope.profile,
        subject_type="market",
        subject_code=chain.subject_code,
        horizon="intraday",
        as_of=envelope.as_of,
        data_cutoff=envelope.data_cutoff,
        snapshot_id=window_snapshot_id,
        decision=decision,
        status=status,
        stop_reason=_briefing_stop_reason(status),
        selected_hypothesis_refs=selected,
        rejected_hypothesis_refs=[],
        minority_opinions=[],
        invalidators=[],
        evidence_ids=list(envelope.evidence_ids),
        unresolved_question_ids=[],
        confidence=result.confidence,
        parent_judgment_ids=list(envelope.parent_judgment_ids),
        relation=envelope.relation,
        config_version=_VERSION,
        model_version=_VERSION,
        prompt_version=_VERSION,
        method_version=_VERSION,
        created_at=envelope.as_of,
    )


def _degraded_judgment(
    *,
    run_id: str,
    profile: str,
    subject_code: str,
    as_of: datetime,
    data_cutoff: datetime,
    snapshot_id: str,
    parent_ids: list[str],
    reason: str,
) -> V4Judgment:
    return V4Judgment(
        judgment_id=briefing_judgment_id(run_id),
        run_id=run_id,
        profile=profile,
        subject_type="market",
        subject_code=subject_code,
        horizon="intraday",
        as_of=as_of,
        data_cutoff=data_cutoff,
        snapshot_id=snapshot_id,
        decision="insufficient",
        status="insufficient",
        stop_reason=reason,
        selected_hypothesis_refs=[],
        rejected_hypothesis_refs=[],
        minority_opinions=[],
        invalidators=[],
        evidence_ids=[],
        unresolved_question_ids=[],
        confidence=0.0,
        parent_judgment_ids=parent_ids,
        relation=None,
        config_version=_VERSION,
        model_version=_VERSION,
        prompt_version=_VERSION,
        method_version=_VERSION,
        created_at=as_of,
    )


def _degraded_briefing_v1(
    profile: BriefingProfile, day: date_type, as_of: datetime
) -> dict[str, object]:
    slot = {
        "briefing_morning": "pre",
        "briefing_midday": "mid",
        "briefing_evening": "post",
    }[profile]
    report_type = {
        "briefing_morning": "morning",
        "briefing_midday": "midday",
        "briefing_evening": "evening",
    }[profile]
    return {
        "schema_version": "briefing.v1",
        "report_type": report_type,
        "slot": slot,
        "date": day.isoformat(),
        "as_of": as_of.isoformat(),
        "display_report": {
            "summary": "数据不足，已降级输出。",
            "highlight": None,
            "sections": [],
            "risks": [],
        },
        "podcast_brief": "数据不足，已降级输出。",
        "audio_path": None,
        "judgment_ids": [],
        "evening_extras": None,
    }


def _briefing_payload(
    *,
    profile: BriefingProfile,
    chain: BriefingChainFixture,
    result: BriefingResult | None,
    judgment: V4Judgment,
    briefing_v1: dict[str, object],
    snapshot_id: str,
    snapshot_sha: str,
    status: LiveStatus,
    usage: dict[str, int],
    audit: dict[str, object],
    missing_fields: list[str],
    legacy_outputs: dict[str, object],
    evidence_ids: list[str],
    window_items: list[dict[str, object]],
    parent_run_id: str | None,
    parent_judgment_ids: list[str],
) -> dict[str, object]:
    # `window_items` 是已投影的证据 dict；账本只序列化，不在运行器里重建契约。
    return {
        "run_id": judgment.run_id,
        "profile": profile,
        "as_of": judgment.as_of.isoformat(),
        "data_cutoff": judgment.data_cutoff.isoformat(),
        "snapshot_id": snapshot_id,
        "snapshot_sha": snapshot_sha,
        "status": status,
        "judgment": _dump(judgment),
        "briefing_v1": briefing_v1,
        "six_factor": None,
        "evidence_ledger": {
            "run_id": judgment.run_id,
            "snapshot_id": snapshot_id,
            "as_of": judgment.as_of.isoformat(),
            "items": window_items,
            "conflict_ids": [],
        },
        "hypotheses": (
            [claim.model_dump(mode="json") for claim in result.claims]
            if result is not None
            else []
        ),
        "gaps": [],
        "revisions": (
            [change.model_dump(mode="json") for change in result.changes]
            if result is not None
            else []
        ),
        "collaboration_mode": "BRIEFING_CHAIN",
        "usage": usage,
        "audit": audit,
        "parent_run_id": parent_run_id,
        "parent_judgment_ids": parent_judgment_ids,
        "legacy_output": legacy_outputs,
        "missing_fields": missing_fields,
        "evidence_ids": evidence_ids,
        "evidence": window_items,
        "chain": chain.model_dump(mode="json"),
    }


def _usage(*, tool_calls: int, evidence_count: int) -> dict[str, int]:
    # 当前 features 链是确定性离线链，没有模型 token；如实记 0，不虚报。
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "tool_calls": tool_calls,
        "evidence_count": evidence_count,
    }


def _audit(
    *,
    mode: str,
    evidence_count: int,
    gap_count: int,
    revision_count: int,
    missing_fields: list[str],
    time_leakage_count: int,
    insufficient_reasons: list[str],
    legacy_output_present: bool,
    model_injected: bool,
    gateway_injected: bool,
    snapshot_sha: str,
) -> dict[str, object]:
    return {
        "mode": mode,
        "evidence_count": evidence_count,
        "gap_count": gap_count,
        "revision_count": revision_count,
        "missing_fields": list(missing_fields),
        "time_leakage_count": time_leakage_count,
        "insufficient_reasons": list(insufficient_reasons),
        "legacy_output_present": legacy_output_present,
        "model_injected": model_injected,
        "gateway_injected": gateway_injected,
        "snapshot_sha": snapshot_sha,
    }


def _dump(value: V4Contract | None) -> object:
    return None if value is None else value.model_dump(mode="json")


async def _archive(result: LiveRunResult, archiver: ArchiveFn) -> LiveRunResult:
    audit = dict(result.audit)
    try:
        outcome = archiver(result.payload)
        if inspect.isawaitable(outcome):
            await cast(Awaitable[object], outcome)
        audit["archived"] = True
    except Exception as exc:  # noqa: BLE001 - 归档失败不推翻已完成的研判
        audit["archived"] = False
        audit["archive_error"] = f"{type(exc).__name__}: {exc}"
    return result.model_copy(update={"audit": audit})


__all__ = [
    "ArchiveFn",
    "LiveRunResult",
    "LiveStatus",
    "reset_live_runs",
    "run_live",
]



