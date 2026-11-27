"""真实 LLM 研究图运行层：把已取到的行情 case 送进 V4 主图。

为什么单独一层：`runtime/runner.py` 负责取数与结果组装，但研究本身必须走
`graph/builder.py` 的 LLM 研究循环（计划 → 取数 → 分析师 → 充分性门禁 →
协作路由 → 单写研判），而不是 `features/market_trace/profile.py` 里那条写死的
确定性脚本。本模块只做三件事：

1. 把 `MarketTraceCase` 的显式时间边界/快照映射成 `ResearchRequest`；
2. 用 case 的真实证据/来源构造竞争假设（不写死固定结论）；
3. `ainvoke` 研究图，并把图状态收敛成可审计的 `ResearchOutcome`。

时间纪律：`as_of` / `data_cutoff` / `snapshot_id` 全部来自 case，不读系统时间。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, cast

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.runnables import RunnableConfig

from ..contracts.deliberation import CaseFile, CollaborationDecision
from ..contracts.evidence import EvidenceItem, EvidenceLedger
from ..contracts.judgment import V4Judgment
from ..contracts.research import (
    CausalEdge,
    Hypothesis,
    ResearchGap,
    ResearchRequest,
    RevisionEvent,
)
from ..features.market_trace.adapter import MarketTraceCase, snapshot_to_evidence
from ..features.market_trace.profile import (
    CONFIG_VERSION,
    METHOD_VERSION,
    MODEL_VERSION,
    PROFILE_NAME,
    PROMPT_VERSION,
)
from ..graph.builder import ResearchGraphState, build_research_graph
from ..tools.gateway import ToolClient, ToolClientResult

#: 六因子因果路径的相邻阶段（与 features 投影的 stage 名一致）。
_STAGE_TRANSITIONS: tuple[tuple[str, str], ...] = (
    ("context", "structural_root"),
    ("structural_root", "trigger"),
    ("trigger", "transmission"),
    ("transmission", "exposure"),
    ("exposure", "repricing"),
    ("repricing", "observable_result"),
)

_HYPOTHESIS_KINDS: dict[str, str] = {
    "hyp_broad": "broad",
    "hyp_style": "style",
    "hyp_sector": "sector",
}

#: 来源 token -> 假设类别；用于把快照证据挂到竞争假设上。
_SUPPORT_TOKENS: dict[str, frozenset[str]] = {
    "broad": frozenset({"INDEX", "BREADTH", "TURNOVER"}),
    "style": frozenset({"STYLE"}),
    "sector": frozenset({"SECTOR"}),
}

_USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "tool_calls")


@dataclass(frozen=True)
class ResearchOutcome:
    """一次真实研究图运行的出口；失败时 status=degraded 且不抛裸异常。"""

    judgment: V4Judgment | None
    hypotheses: list[Hypothesis]
    evidence_ledger: EvidenceLedger | None
    gaps: list[ResearchGap]
    revision_events: list[RevisionEvent]
    collaboration_mode: str
    sufficiency_passed: bool
    usage: dict[str, object]
    status: str
    errors: list[dict[str, object]] = field(default_factory=list)
    graph_trace: list[dict[str, object]] = field(default_factory=list)
    causal_edges: list[CausalEdge] = field(default_factory=list)


def _as_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _as_str(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _change_pct(info: Mapping[str, object]) -> float | None:
    for key in ("change_pct", "changePercent", "pct_chg", "change_percent"):
        value = _as_float(info.get(key))
        if value is not None:
            return value
    return None


def _kind(hypothesis_id: str) -> str:
    return _HYPOTHESIS_KINDS.get(hypothesis_id, "")


def build_market_trace_request(
    case: MarketTraceCase, *, run_id: str | None = None
) -> ResearchRequest:
    """把真实 case 映射成研究请求；时间边界全部取自 case。"""
    effective_run_id = run_id or case.run_id
    return ResearchRequest(
        request_id=f"req_{effective_run_id}",
        run_id=effective_run_id,
        profile=PROFILE_NAME,
        subject_type="market",
        subject_code=case.subject_code,
        research_goal="Which root cause explains the index move?",
        required_questions=["structural_root", "trigger", "amplifier"],
        horizon="1d",
        as_of=case.as_of,
        data_cutoff=case.data_cutoff,
        snapshot_id=case.snapshot.snapshot_id,
        config_version=CONFIG_VERSION,
        model_version=MODEL_VERSION,
        prompt_version=PROMPT_VERSION,
        method_version=METHOD_VERSION,
        budget_policy=PROFILE_NAME,
        stop_policy="default",
    )


def _index_view(snapshot: Any) -> tuple[str | None, float | None]:
    indexes = _as_mapping(snapshot.a_share.get("indexes")) or {}
    for code in sorted(indexes):
        info = _as_mapping(indexes[code])
        if info is None:
            continue
        return _as_str(info.get("name")) or code, _change_pct(info)
    return None, None


def _breadth_view(snapshot: Any) -> tuple[int | None, int | None]:
    breadth = _as_mapping(snapshot.a_share.get("breadth")) or {}
    return _as_int(breadth.get("advance_count")), _as_int(breadth.get("decline_count"))


def _style_view(snapshot: Any) -> tuple[float | None, float | None]:
    style = _as_mapping(snapshot.a_share.get("style")) or {}
    large = _as_float(
        style.get("large_cap_change_pct", style.get("large_cap_pct"))
    )
    small = _as_float(
        style.get("small_cap_change_pct", style.get("small_cap_pct"))
    )
    return large, small


def _top_sector(snapshot: Any) -> tuple[str | None, float | None]:
    sectors = _as_mapping(snapshot.a_share.get("sectors")) or {}
    rows: list[tuple[str, float | None]] = []
    for name in sorted(sectors):
        info = _as_mapping(sectors[name])
        if info is not None:
            rows.append((name, _change_pct(info)))
    if not rows:
        return None, None
    ranked = sorted(rows, key=lambda item: (item[1] is None, -(item[1] or 0.0), item[0]))
    return ranked[0]


def build_market_trace_hypotheses(case: MarketTraceCase) -> list[Hypothesis]:
    """按 case 的真实证据/来源构造 3 个竞争假设。

    statement 只由已取到的行情事实生成，因此不同 case 会得到不同命题；
    这里不复制旧链“流动性是放大器”这类固定结论。
    """
    snapshot = case.snapshot
    name, change = _index_view(snapshot)
    advance, decline = _breadth_view(snapshot)
    large, small = _style_view(snapshot)
    sector_name, sector_change = _top_sector(snapshot)

    if name is not None and change is not None and advance is not None and decline is not None:
        broad_statement = (
            f"{name}收盘 {change:+.2f}%（涨 {advance}/跌 {decline}），广度驱动是主要解释"
        )
    elif name is not None and change is not None:
        broad_statement = f"{name}收盘 {change:+.2f}%，广度驱动是主要解释"
    else:
        broad_statement = f"{case.subject_code}行情数据缺失，广度驱动待检验"

    if large is not None and small is not None:
        style_statement = (
            f"风格分化（大盘 {large:+.2f}% vs 小盘 {small:+.2f}%）是主要驱动"
        )
    else:
        style_statement = "风格分化是待检验的竞争解释"

    if sector_name is not None and sector_change is not None:
        sector_statement = f"板块轮动（{sector_name} {sector_change:+.2f}%）是次要因素"
    else:
        sector_statement = "板块轮动是待检验的次级解释"

    return [
        Hypothesis(
            hypothesis_id="hyp_broad",
            version=1,
            statement=broad_statement,
            status="open",
            prior_confidence=0.60,
            assumptions=["指数与涨跌家数同步反映市场广度"],
            disconfirmers=["若指数与涨跌家数明显背离"],
            required_evidence_types=[],
            scope="market",
        ),
        Hypothesis(
            hypothesis_id="hyp_style",
            version=1,
            statement=style_statement,
            status="open",
            prior_confidence=0.45,
            assumptions=["大小盘相对强弱可解释主要波动"],
            disconfirmers=["若大小盘同步同向变动"],
            required_evidence_types=[],
            scope="market",
        ),
        Hypothesis(
            hypothesis_id="hyp_sector",
            version=1,
            statement=sector_statement,
            status="open",
            prior_confidence=0.35,
            assumptions=["板块涨跌出现分化"],
            disconfirmers=["若全部板块同涨同跌"],
            required_evidence_types=[],
            scope="market",
        ),
    ]


def _link_evidence(
    items: Sequence[EvidenceItem], hypotheses: Sequence[Hypothesis]
) -> list[EvidenceItem]:
    """把快照证据按来源 token 挂到竞争假设上（V4 自己的研究步骤）。"""
    linked: list[EvidenceItem] = []
    for item in items:
        raw_source_id = item.tool_params.get("source_id", "")
        tokens = frozenset(str(raw_source_id).upper().split("_"))
        refs = sorted(
            hypothesis.ref
            for hypothesis in hypotheses
            if tokens & _SUPPORT_TOKENS.get(_kind(hypothesis.hypothesis_id), frozenset())
        )
        linked.append(item.model_copy(update={"supports_hypothesis_refs": refs}))
    return linked


def _case_file(
    case: MarketTraceCase,
    request: ResearchRequest,
    hypotheses: Sequence[Hypothesis],
    items: Sequence[EvidenceItem],
) -> CaseFile:
    return CaseFile(
        case_id=case.case_id,
        run_id=request.run_id,
        profile=PROFILE_NAME,
        case_version=1,
        question="Which root cause explains the index move?",
        decision_type="market_root_cause",
        materiality=case.materiality,
        subject="market",
        horizon="1d",
        as_of=case.as_of,
        snapshot_id=case.snapshot.snapshot_id,
        frozen_fact_ids=sorted(item.evidence_id for item in items),
        hypothesis_refs=sorted(hypothesis.ref for hypothesis in hypotheses),
        open_issue_ids=sorted(
            f"issue_{hypothesis.hypothesis_id}" for hypothesis in hypotheses
        ),
        deadline=case.as_of + timedelta(hours=1),
        budget_policy=PROFILE_NAME,
        decision_rubric_version="rubric_v1",
    )


def _initial_state(
    case: MarketTraceCase,
    request: ResearchRequest,
    hypotheses: Sequence[Hypothesis],
) -> dict[str, Any]:
    raw_items = snapshot_to_evidence(
        case.snapshot, case.as_of, case.data_cutoff, case.snapshot.snapshot_id
    )
    items = _link_evidence(raw_items, hypotheses)
    ledger = EvidenceLedger(
        run_id=request.run_id,
        snapshot_id=request.snapshot_id,
        as_of=request.as_of,
        items=sorted(items, key=lambda item: item.evidence_id),
        conflict_ids=[],
    )
    return {
        "request": request,
        "case_file": _case_file(case, request, hypotheses, items),
        "evidence_ledger": ledger,
        # 分析师任务文本要写清“证据是什么”，而不是只给 ID；这里把冻结快照的
        # 来源正文按 source_id 透传，供 analysts/context.py 生成摘要。
        "evidence_sources": {
            source_id: record.content
            for source_id, record in case.snapshot.sources.items()
        },
        "hypotheses": list(hypotheses),
        "causal_graph": [],
        "research_gaps": [],
        "round_ledgers": [],
        "publish_ready": False,
        "counter_evidence_coverage": 1.0,
        "parent_judgment_ids": [],
        # 研究图状态里以 request 为准；这里显式冗余也便于观测/调试。
        "as_of": case.as_of,
        "data_cutoff": case.data_cutoff,
        "snapshot_id": case.snapshot.snapshot_id,
        "usage": [],
        "errors": [],
        "trace": [],
    }


def _aggregate_usage(records: Iterable[object]) -> dict[str, object]:
    """从图状态 usage 聚合 token/tool 计数；模型未回报时如实标 unavailable。"""
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
            totals[key] = totals.get(key, 0) + float(value)

    analyst_tokens = int(totals.get("analyst_tokens", 0))
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": analyst_tokens,
        "tool_calls": int(totals.get("tool_calls", 0)),
        "token_source": "graph_analyst_tokens" if analyst_tokens > 0 else "unavailable",
    }


def _derive_causal_edges(
    hypotheses: Sequence[Hypothesis],
    ledger: EvidenceLedger | None,
    *,
    valid_time: datetime,
) -> list[CausalEdge]:
    """由最终假设与证据派生六因子因果路径；不新增任何证据。"""
    items = list(ledger.items) if ledger is not None else []
    edges: list[CausalEdge] = []
    for hypothesis in sorted(hypotheses, key=lambda item: item.ref):
        support = sorted(
            item.evidence_id
            for item in items
            if hypothesis.ref in item.supports_hypothesis_refs
        )
        for _from_node, to_node in _STAGE_TRANSITIONS:
            edges.append(
                CausalEdge(
                    edge_id=f"edge_{hypothesis.hypothesis_id}_{to_node}",
                    from_node=_from_node,
                    to_node=to_node,
                    mechanism=f"{to_node}: {hypothesis.statement}",
                    supporting_evidence_ids=list(support),
                    contradicting_evidence_ids=[],
                    edge_type="inference" if support else "hypothesis",
                    confidence=hypothesis.prior_confidence,
                    valid_time=valid_time,
                    magnitude_check=f"evidence_count:{len(support)}",
                    counterfactual_check=f"invalidator:{hypothesis.disconfirmers[0]}",
                )
            )
    return edges


def _build_thinking_model_safe() -> BaseChatModel | None:
    """构造启用 thinking 的终局推理模型；失败返回 None 并保持主流程可用。"""
    try:
        from aistock_agent.brain_v4.analysts.model_factory import (
            build_thinking_model,
        )

        return build_thinking_model()
    except Exception:  # noqa: BLE001 - thinking 只是增强项，失败不应拖垮 run
        return None


def _degraded_outcome(
    exc: BaseException, *, hypotheses: Sequence[Hypothesis]
) -> ResearchOutcome:
    return ResearchOutcome(
        judgment=None,
        hypotheses=list(hypotheses),
        evidence_ledger=None,
        gaps=[],
        revision_events=[],
        collaboration_mode="DEGRADED",
        sufficiency_passed=False,
        usage={
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "tool_calls": 0,
            "token_source": "unavailable",
        },
        status="degraded",
        errors=[
            {
                "error_id": "research_graph_error",
                "error_kind": type(exc).__name__,
                "detail": str(exc),
            }
        ],
        graph_trace=[],
        causal_edges=[],
    )


def _outcome_from_state(
    state: Mapping[str, Any], case: MarketTraceCase
) -> ResearchOutcome:
    raw_judgment = state.get("judgment_draft")
    judgment = raw_judgment if isinstance(raw_judgment, V4Judgment) else None

    raw_ledger = state.get("evidence_ledger")
    ledger = raw_ledger if isinstance(raw_ledger, EvidenceLedger) else None

    hypotheses = [
        item
        for item in state.get("hypotheses", ())
        if isinstance(item, Hypothesis)
    ]
    gaps = [
        item
        for item in state.get("research_gaps", ())
        if isinstance(item, ResearchGap)
    ]
    revisions = [
        item
        for item in state.get("revision_events", ())
        if isinstance(item, RevisionEvent)
    ]
    raw_collab = state.get("collaboration_decision")
    collaboration_mode = (
        raw_collab.mode if isinstance(raw_collab, CollaborationDecision) else "UNKNOWN"
    )
    errors = [
        dict(item)
        for item in state.get("errors", ())
        if isinstance(item, Mapping)
    ]
    trace = [
        dict(item)
        for item in state.get("trace", ())
        if isinstance(item, Mapping)
    ]
    usage = _aggregate_usage(state.get("usage", ()))

    if judgment is None:
        return ResearchOutcome(
            judgment=None,
            hypotheses=hypotheses,
            evidence_ledger=ledger,
            gaps=gaps,
            revision_events=revisions,
            collaboration_mode=collaboration_mode,
            sufficiency_passed=bool(state.get("sufficiency_passed", False)),
            usage=usage,
            status="degraded",
            errors=errors or [{"error_kind": "judgment_missing"}],
            graph_trace=trace,
            causal_edges=[],
        )

    causal_edges = _derive_causal_edges(
        hypotheses, ledger, valid_time=case.data_cutoff
    )
    return ResearchOutcome(
        judgment=judgment,
        hypotheses=hypotheses,
        evidence_ledger=ledger,
        gaps=gaps,
        revision_events=revisions,
        collaboration_mode=collaboration_mode,
        sufficiency_passed=bool(state.get("sufficiency_passed", False)),
        usage=usage,
        status="ok",
        errors=errors,
        graph_trace=trace,
        causal_edges=causal_edges,
    )


async def run_research_graph(
    case: MarketTraceCase,
    *,
    model: BaseChatModel | None,
    tool_client: ToolClient,
    gateway: object | None = None,
) -> ResearchOutcome:
    """执行一次真实 V4 研究图；任何执行失败都收敛为 degraded，不抛裸异常。"""
    # gateway 由主图内部按 profile 构造；保留参数以对齐 P4 调度签名。
    _ = gateway
    request = build_market_trace_request(case)
    hypotheses = build_market_trace_hypotheses(case)
    initial = _initial_state(case, request, hypotheses)

    try:
        # 终局推理模型：仅在有真实分析师模型时启用；构造失败不影响主流程
        # （thinking 只提升深度，不应成为可用性单点）。
        thinking_model = _build_thinking_model_safe() if model is not None else None
        graph = build_research_graph(
            analyst_model=model,
            tool_client=tool_client,
            thinking_model=thinking_model,
        )
        final = await graph.ainvoke(
            cast(ResearchGraphState, initial),
            config=cast(RunnableConfig, {"recursion_limit": 1000}),
        )
    except Exception as exc:  # noqa: BLE001 - 图执行失败必须降级而不是抛裸异常
        return _degraded_outcome(exc, hypotheses=hypotheses)

    return _outcome_from_state(final, case)


def _resolve_day(params: Mapping[str, Any], default: datetime) -> date:
    raw = params.get("date")
    if isinstance(raw, str) and raw.strip():
        text = raw.strip().replace("/", "-")
        try:
            return date.fromisoformat(text)
        except ValueError:
            compact = text.replace("-", "")
            if len(compact) == 8 and compact.isdigit():
                return date(
                    int(compact[0:4]), int(compact[4:6]), int(compact[6:8])
                )
    return default.date()


class LiveMarketToolClient:
    """把 V4 工具名映射到 `NodeDataSource` 只读方法。

    主图的 `research_task` 按 profile 工具组（如 `market_snapshot`）扇出，
    而分析师绑定的是展开后的原子工具；两类名字都必须在同一客户端里解析。
    时间口径：日期优先取工具参数，否则取 case 的 `as_of` 当天，绝不读系统时间。
    """

    def __init__(
        self,
        client: object,
        *,
        as_of: datetime,
        data_cutoff: datetime,
        subject_code: str,
    ) -> None:
        self._client = client
        self._as_of = as_of
        self._data_cutoff = data_cutoff
        self._subject_code = subject_code

    async def fetch(self, tool: str, params: Mapping[str, Any]) -> ToolClientResult:
        day = _resolve_day(params, self._as_of)
        data = await self._load(tool.strip().lower(), day)
        payload: object = data if isinstance(data, Mapping) else {"items": data}
        return ToolClientResult(
            data=payload,
            published_at=self._data_cutoff,
            event_time=self._data_cutoff,
            quality="high",
        )

    async def _load(self, name: str, day: date) -> object:
        if name == "market_snapshot":
            compact = day.strftime("%Y%m%d")
            data = await self._call("get_close_snapshot", compact)
            if data is None:
                data = await self._call("get_last_close_snapshot")
        elif name == "intraday_sectors":
            data = await self._call("get_intraday_sectors")
        elif name in {"calendar_events", "event_evidence", "news"}:
            iso = day.isoformat()
            data = await self._call("get_calendar_events", iso, iso)
        elif name == "ths_index_map":
            data = await self._call("get_ths_index_map")
        elif name in {"index_kline", "trend", "stock_kline"}:
            data = await self._call("get_index_kline", self._subject_code)
        elif name in {"quote", "realtime_quote"}:
            data = await self._call("get_quote", self._subject_code)
        else:
            raise RuntimeError(f"tool {name!r} has no Node endpoint")
        if data is None:
            raise RuntimeError(f"tool {name!r} returned no data")
        return data

    async def _call(self, method: str, *args: object) -> object:
        func = getattr(self._client, method, None)
        if not callable(func):
            raise RuntimeError(f"client is missing method {method!r}")
        return await func(*args)


__all__ = [
    "LiveMarketToolClient",
    "ResearchOutcome",
    "build_market_trace_hypotheses",
    "build_market_trace_request",
    "run_research_graph",
]
