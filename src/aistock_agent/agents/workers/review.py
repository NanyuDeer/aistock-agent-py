"""Review Agent — 收盘溯源归因（受限 JSON 推理）

模式：单次 get_deep_think().ainvoke，输入系统提示词 + JSON 快照
校验：MarketTraceResult.model_validate_json + validate_trace_against_snapshot
缓存：Redis TTL=2小时（briefing:review:YYYY-MM-DD）
归档：docs/agent-outputs/review/YYYY-MM-DD-HHMM-review.md

不再使用 ReAct 模式或工具调用。事实来自
build_market_trace_snapshot 冻结的 MarketTraceSnapshot，LLM 只做归因推理。
"""

import re
from datetime import datetime

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.review import REVIEW_PROMPT
from aistock_agent.schemas.market_trace import (
    CandidateExplanation,
    MarketTraceResult,
    MarketTraceSnapshot,
    ReviewArtifact,
    SourceRecord,
)
from aistock_agent.services.archiver import (
    archive_market_trace_snapshot,
    archive_review,
)
from aistock_agent.services.cache import get_cached_review, set_cached_review
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think
from aistock_agent.services.market_trace_snapshot import build_market_trace_snapshot
from aistock_agent.state.schema import AgentState

logger = structlog.get_logger()

# ============================================================================
# 常量
# ============================================================================

# 4 个固定候选类别 — CandidateExplanation.id 必须与 category 同名
REQUIRED_CANDIDATE_CATEGORIES: set[str] = {
    "global_risk_liquidity",
    "domestic_macro_policy",
    "industry_technology_supply",
    "market_positioning_liquidity",
}

# primary/alternative chain 必须按顺序包含的 6 个阶段
REQUIRED_CHAIN_STAGES: list[str] = [
    "structural_root",
    "trigger",
    "transmission",
    "exposure",
    "repricing",
    "observable_result",
]

# 降级文本 — 校验失败、LLM 异常、快照构建失败时返回
DEGRADED_RESPONSE = "收盘溯源生成暂时不可用，请稍后重试"

# 代码围栏剥离 — 防御性处理 LLM 可能包裹的 ```json ... ```
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


def _strip_code_fences(text: str) -> str:
    """剥离 LLM 可能包裹的 ```json ... ``` 代码围栏。"""
    m = _CODE_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


# ============================================================================
# 跨对象校验 — 在 model_validate_json 之后做结构性检查
# ============================================================================


def validate_selected_chain_ids(trace: MarketTraceResult) -> None:
    """检查 primary_chain_id / alternative_chain_id 指向的候选存在且 status 符合要求。

    - 存在 supported 候选时，primary_chain_id 必须指向其中之一，不得为 null
    - 无 supported 候选时（全部 insufficient/rejected），primary_chain_id 必须为 null
    - alternative_chain_id 必须指向不同的 supported 或 weak 候选；null 允许
    - primary 与 alternative 不得相同
    """
    candidate_by_id = {c.id: c for c in trace.candidates}
    has_supported = any(c.status == "supported" for c in trace.candidates)

    if trace.primary_chain_id is None:
        if has_supported:
            raise ValueError(
                "primary_chain_id must not be null when supported candidates exist"
            )
        # 无 supported 候选（全部 insufficient/rejected）：primary_chain_id=null 正确
        return

    primary = candidate_by_id.get(trace.primary_chain_id)
    if primary is None:
        raise ValueError(f"unknown primary_chain_id: {trace.primary_chain_id}")
    if primary.status != "supported":
        raise ValueError(
            f"primary_chain_id points to non-supported candidate: "
            f"{primary.id} ({primary.status})"
        )

    if trace.alternative_chain_id is not None:
        if trace.alternative_chain_id == trace.primary_chain_id:
            raise ValueError("alternative_chain_id equals primary_chain_id")
        alternative = candidate_by_id.get(trace.alternative_chain_id)
        if alternative is None:
            raise ValueError(
                f"unknown alternative_chain_id: {trace.alternative_chain_id}"
            )
        if alternative.status not in {"supported", "weak"}:
            raise ValueError(
                f"alternative_chain_id points to invalid status: "
                f"{alternative.id} ({alternative.status})"
            )


def validate_chain_stages(trace: MarketTraceResult) -> None:
    """primary 和非空 alternative 指向的 chain 必须恰好包含 6 个阶段（按顺序）。

    rejected/insufficient 候选可以没有 chain（chain=None）。
    无论 primary 是否为 null，非空 alternative 都必须通过 6 阶段校验：
    - primary_chain_id=None 时跳过 primary chain 校验；
    - alternative_chain_id 非空时仍校验 alternative chain 的 6 阶段顺序。
    """
    candidate_by_id = {c.id: c for c in trace.candidates}

    if trace.primary_chain_id is not None:
        primary = candidate_by_id.get(trace.primary_chain_id)
        if primary is None or primary.chain is None:
            raise ValueError("primary candidate has no chain")
        primary_stages = [n.stage for n in primary.chain.nodes]
        if primary_stages != REQUIRED_CHAIN_STAGES:
            raise ValueError(
                f"primary chain stages mismatch: {primary_stages} != {REQUIRED_CHAIN_STAGES}"
            )

    # 无论 primary 是否为 null，非空 alternative 都必须通过 6 阶段校验。
    # 修复前：primary_chain_id=None 时直接 return，跳过 alternative 校验，
    # 导致 primary=null + alternative 链条不完整的非法 trace 通过校验。
    if trace.alternative_chain_id is not None:
        alt = candidate_by_id.get(trace.alternative_chain_id)
        if alt is None or alt.chain is None:
            raise ValueError("alternative candidate has no chain")
        alt_stages = [n.stage for n in alt.chain.nodes]
        if alt_stages != REQUIRED_CHAIN_STAGES:
            raise ValueError(
                f"alternative chain stages mismatch: "
                f"{alt_stages} != {REQUIRED_CHAIN_STAGES}"
            )


def validate_trace_against_snapshot(
    trace: MarketTraceResult,
    snapshot: MarketTraceSnapshot,
) -> None:
    """跨对象校验：候选完整性、source_id 存在性、chain 选择与阶段、归因一致性。

    新增 4 类校验（Task 5 review 修复）：
    1. trace.dominant_phenomenon 与 snapshot.dominant_phenomenon 一致；
       两者同时为 null 或同时非 null 且 kind 一致。
    2. dominant_phenomenon.fact_ids 必须全部存在于 snapshot.sources。
    3. 每个因果节点的 evidence_ids 不得为空，且全部存在于 snapshot.sources。
    4. observable_result 节点必须至少引用一个 kind="market_fact" 的事实。
    """
    categories = {candidate.category for candidate in trace.candidates}
    if (
        len(trace.candidates) != 4
        or categories != REQUIRED_CANDIDATE_CATEGORIES
        or {candidate.id for candidate in trace.candidates} != categories
    ):
        raise ValueError("candidate categories are incomplete")

    # 1. trace.dominant_phenomenon 与 snapshot.dominant_phenomenon 一致
    snapshot_dp = snapshot.dominant_phenomenon
    trace_dp = trace.dominant_phenomenon
    if trace_dp is None and snapshot_dp is not None:
        raise ValueError(
            "trace.dominant_phenomenon is null but snapshot.dominant_phenomenon is not"
        )
    if trace_dp is not None and snapshot_dp is None:
        raise ValueError(
            "trace.dominant_phenomenon is not null but snapshot.dominant_phenomenon is null"
        )
    if trace_dp is not None and snapshot_dp is not None:
        if trace_dp.kind != snapshot_dp.kind:
            raise ValueError(
                f"trace.dominant_phenomenon.kind {trace_dp.kind} != "
                f"snapshot.dominant_phenomenon.kind {snapshot_dp.kind}"
            )
        # 2. dominant_phenomenon.fact_ids 必须全部存在于 snapshot.sources
        for fact_id in trace_dp.fact_ids:
            if fact_id not in snapshot.sources:
                raise ValueError(
                    f"trace.dominant_phenomenon.fact_ids references unknown source_id: {fact_id}"
                )

    # 3. 每个候选的证据引用必须存在；每个非空 chain 节点必须有至少 1 个证据
    ids = set(snapshot.sources)
    for candidate in trace.candidates:
        for source_id in (
            candidate.supporting_evidence_ids
            + candidate.counter_evidence_ids
        ):
            if source_id not in ids:
                raise ValueError(f"unknown source_id: {source_id}")
        if candidate.chain is not None:
            for node in candidate.chain.nodes:
                if not node.evidence_ids:
                    raise ValueError(
                        f"candidate {candidate.id} node {node.stage} has empty evidence_ids"
                    )
                for source_id in node.evidence_ids:
                    if source_id not in ids:
                        raise ValueError(f"unknown source_id: {source_id}")

    # 4. observable_result 节点必须至少引用一个 kind="market_fact" 的事实
    #    （仅对 primary 和非空 alternative 指向的 chain 校验；
    #     rejected/insufficient 候选可以没有 chain）
    candidate_by_id = {c.id: c for c in trace.candidates}
    chains_to_check: list[tuple[str, CandidateExplanation]] = []
    if trace.primary_chain_id is not None:
        primary = candidate_by_id.get(trace.primary_chain_id)
        if primary is not None and primary.chain is not None:
            chains_to_check.append(("primary", primary))
    if trace.alternative_chain_id is not None:
        alt = candidate_by_id.get(trace.alternative_chain_id)
        if alt is not None and alt.chain is not None:
            chains_to_check.append(("alternative", alt))
    for label, candidate in chains_to_check:
        chain = candidate.chain
        if chain is None:
            continue
        for node in chain.nodes:
            if node.stage == "observable_result":
                has_market_fact = any(
                    _source_kind(snapshot.sources, sid) == "market_fact"
                    for sid in node.evidence_ids
                )
                if not has_market_fact:
                    raise ValueError(
                        f"{label} candidate {candidate.id} observable_result must "
                        f"reference at least one market_fact source"
                    )

    validate_selected_chain_ids(trace)
    validate_chain_stages(trace)


def _source_kind(
    sources: dict[str, SourceRecord],
    source_id: str,
) -> str | None:
    """安全取 SourceRecord.kind 的字符串值，找不到时返回 None。"""
    record = sources.get(source_id)
    return record.kind if record is not None else None


# ============================================================================
# Markdown 渲染 — 从已验证的 JSON 工件渲染展示层
# ============================================================================


def _extract_sectors_from_snapshot(snapshot: MarketTraceSnapshot) -> list[str]:
    """从 snapshot.a_share.sectors 提取板块名（去重保序）。

    a_share.sectors 结构：
      {"top_gainers": [{"name": "...", ...}, ...],
       "top_losers":  [{"name": "...", ...}, ...],
       "top_inflows":  [{"name": "...", ...}, ...],
       "top_outflows": [{"name": "...", ...}, ...]}
    """
    sectors_raw = snapshot.a_share.get("sectors")
    if not isinstance(sectors_raw, dict):
        return []
    seen: set[str] = set()
    result: list[str] = []
    for key in ("top_gainers", "top_losers", "top_inflows", "top_outflows"):
        items = sectors_raw.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if isinstance(name, str) and name and name not in seen:
                seen.add(name)
                result.append(name)
    return result


def _format_source_time(source: SourceRecord) -> str:
    """格式化 SourceRecord.occurred_at 为可读时间。"""
    if source.occurred_at is None:
        return "未知时间"
    try:
        return source.occurred_at.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(source.occurred_at)


def _render_candidate(candidate: CandidateExplanation, indent: str = "") -> list[str]:
    """渲染单个 CandidateExplanation 为 Markdown 行列表。"""
    lines: list[str] = []
    lines.append(f"{indent}### {candidate.category}（{candidate.status}）")
    lines.append(f"{indent}- 结论：{candidate.verdict}")
    if candidate.chain:
        for node in candidate.chain.nodes:
            lines.append(f"{indent}- **{node.stage}**：{node.claim}")
            if node.evidence_ids:
                lines.append(f"{indent}  - 证据：{', '.join(node.evidence_ids)}")
    if candidate.supporting_evidence_ids:
        lines.append(
            f"{indent}- 支持证据：{', '.join(candidate.supporting_evidence_ids)}"
        )
    if candidate.counter_evidence_ids:
        lines.append(
            f"{indent}- 反证：{', '.join(candidate.counter_evidence_ids)}"
        )
    return lines


def _collect_referenced_source_ids(trace: MarketTraceResult) -> list[str]:
    """收集 trace 中所有引用过的 source_id（按出现顺序去重）。"""
    referenced: list[str] = []
    seen: set[str] = set()
    for candidate in trace.candidates:
        for sid in (
            candidate.supporting_evidence_ids + candidate.counter_evidence_ids
        ):
            if sid not in seen:
                seen.add(sid)
                referenced.append(sid)
        if candidate.chain:
            for node in candidate.chain.nodes:
                for sid in node.evidence_ids:
                    if sid not in seen:
                        seen.add(sid)
                        referenced.append(sid)
    return referenced


def render_market_trace_markdown(
    trace: MarketTraceResult,
    snapshot: MarketTraceSnapshot,
) -> str:
    """从已验证的 trace + snapshot 渲染展示层 Markdown。

    Markdown 是展示层，不是事实源。固定章节来自 brief Step 4。
    """
    candidate_by_id = {c.id: c for c in trace.candidates}
    primary = (
        candidate_by_id.get(trace.primary_chain_id)
        if trace.primary_chain_id
        else None
    )
    alternative = (
        candidate_by_id.get(trace.alternative_chain_id)
        if trace.alternative_chain_id
        else None
    )
    selected_ids = {trace.primary_chain_id, trace.alternative_chain_id}
    rejected_or_insufficient = [
        c for c in trace.candidates if c.id not in selected_ids
    ]

    lines: list[str] = []
    lines.append(f"# A股收盘溯源｜{snapshot.trade_date}")
    lines.append(f"快照编号：{snapshot.snapshot_id}")
    lines.append("")

    # 主导现象
    lines.append("## 主导现象")
    if trace.dominant_phenomenon:
        dp = trace.dominant_phenomenon
        lines.append(f"- 类型：{dp.kind}")
        lines.append(f"- 摘要：{dp.summary}")
        lines.append(f"- 评分：{dp.score}")
        if dp.fact_ids:
            lines.append(f"- 事实 ID：{', '.join(dp.fact_ids)}")
    else:
        lines.append("- 无明确主导现象，未强行归因。")
    lines.append("")

    # 主因果链
    lines.append("## 主因果链")
    if primary:
        lines.extend(_render_candidate(primary))
    else:
        lines.append("- 证据不足，未确认主因。")
    lines.append("")

    # 备选解释
    lines.append("## 备选解释")
    if alternative:
        lines.extend(_render_candidate(alternative))
    else:
        lines.append("- 无备选解释。")
    lines.append("")

    # 已排除或证据不足的解释
    lines.append("## 已排除或证据不足的解释")
    if rejected_or_insufficient:
        for c in rejected_or_insufficient:
            lines.extend(_render_candidate(c))
    else:
        lines.append("- 无。")
    lines.append("")

    # 证据索引
    lines.append("## 证据索引")
    referenced_ids = _collect_referenced_source_ids(trace)
    if referenced_ids:
        for sid in referenced_ids:
            source = snapshot.sources.get(sid)
            if source:
                occurred = _format_source_time(source)
                url = source.url or "无 URL"
                lines.append(
                    f"- [{sid}] {source.provider}｜{source.title}｜{occurred}｜{url}"
                )
    else:
        lines.append("- 无引用证据。")
    lines.append("")

    # 未解问题
    lines.append("## 未解问题")
    if trace.unresolved_questions:
        for q in trace.unresolved_questions:
            lines.append(f"- {q}")
    else:
        lines.append("- 无。")
    lines.append("")

    # 板块列表（保留 SECTOR_LIST 标记，供 _build_review_report 提取）
    lines.append("<!--SECTOR_LIST_START-->")
    for name in _extract_sectors_from_snapshot(snapshot):
        lines.append(f"- {name}")
    lines.append("<!--SECTOR_LIST_END-->")

    return "\n".join(lines)


# ============================================================================
# 持久化辅助 — Task 5：先归档事实，再缓存/持久化
# ============================================================================

# --- markdown 解析辅助：纯文本正则，不引入 LLM 调用 ---
# 主导现象（新 markdown 格式：render_market_trace_markdown 产出的 ## 主导现象 段，
# 标题行之后到下一个 '## ' 标题前的内容作为摘要来源）
_DOMINANT_PHENOMENON_RE = re.compile(
    r"##\s*主导现象[^\n]*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)
# 步骤4：输出核心结论（旧 markdown 格式，保留以兼容已缓存的旧报告）
_STEP_FOUR_RE = re.compile(
    r"##\s*步骤?\s*4[：:：]?\s*输出核心结论[^\n]*\n(.*?)(?=\n##\s|\Z)",
    re.DOTALL,
)
# 步骤5 内显式标记的板块列表
_SECTOR_LIST_RE = re.compile(
    r"<!--\s*SECTOR_LIST_START\s*-->(.*?)<!--\s*SECTOR_LIST_END\s*-->",
    re.DOTALL,
)
# 附录B 板块表现矩阵：跳过表头与分隔行，取数据行第一列（板块名称）
_APPENDIX_B_RE = re.compile(
    r"##\s*附录\s*B[：:：]?\s*板块表现矩阵[^\n]*\n"
    r"(?:\|[^\n]*\|\n)?"                 # 可选的表头行
    r"(?:\s*\|?\s*[-: ]+\s*\|[^\n]*\n)?"  # 分隔行（|---|---|...）
    r"((?:\|[^\n]+\n?)+)",
)


def _first_effective_line(text: str) -> str:
    """从一段文本中取首个非空、非markdown符号的有效行作为摘要。"""
    for raw in text.splitlines():
        line = raw.strip().lstrip("-*#>").strip()
        if line:
            return line
    return ""


def _extract_trace_summary(markdown: str) -> str:
    """从复盘 markdown 提取摘要（主导现象段首个有效行）。

    摘要提取顺序：
    1. ``## 主导现象`` 段落的首个有效行（新 markdown 格式，render_market_trace_markdown 产出）
    2. ``## 步骤4`` 段落的首个有效行（旧 markdown 格式，兼容已缓存的旧报告）
    3. 整段 markdown 的首个有效行（兜底，避免下游拿到空字符串）
    """
    summary = ""
    m = _DOMINANT_PHENOMENON_RE.search(markdown)
    if m:
        summary = _first_effective_line(m.group(1))
    if not summary:
        m = _STEP_FOUR_RE.search(markdown)
        if m:
            summary = _first_effective_line(m.group(1))
    if not summary:
        summary = _first_effective_line(markdown)
    return summary


def _extract_review_sectors(markdown: str) -> list[str]:
    """从 markdown 提取板块列表：优先 SECTOR_LIST 标记；退化到附录B 表格第一列。"""
    m = _SECTOR_LIST_RE.search(markdown)
    if m:
        sectors: list[str] = []
        for line in m.group(1).splitlines():
            name = line.strip().lstrip("-*").strip()
            if name:
                sectors.append(name)
        if sectors:
            return sectors

    m = _APPENDIX_B_RE.search(markdown)
    if m:
        sectors = []
        for row in m.group(1).splitlines():
            # 表格行形如 "| 黄金 | +3.5% | ..."
            cells = [c.strip() for c in row.strip().strip("|").split("|")]
            if cells and cells[0] and cells[0] != "板块名称":
                sectors.append(cells[0])
        if sectors:
            return sectors

    return []


def _build_review_report(artifact: ReviewArtifact) -> dict[str, object]:
    """把已验证的 ReviewArtifact 封装成 schema v2 的持久化结构。

    schema v2：display_report 提供前端直接消费的字段，details 保留原始 markdown，
    market_trace 提供完整的事实快照与归因 trace，供下游快照构建器复用。
    不触发任何 LLM 调用——所有摘要/板块均从 artifact 中直接取值。
    """
    content: dict[str, object] = {
        "display_report": {
            "summary": artifact.trace_summary,
            "details": artifact.markdown,
            "stocks": [],
            "sectors": artifact.sectors,
            "risks": artifact.trace.unresolved_questions,
        },
        "podcast_brief": "",
        "schema_version": "2.0",
        "snapshot_id": artifact.snapshot.snapshot_id,
        "market_trace": {
            "snapshot": artifact.snapshot.model_dump(mode="json"),
            "trace": artifact.trace.model_dump(mode="json"),
        },
    }
    return content


async def _persist_review_report(
    state: AgentState,
    artifact: ReviewArtifact,
) -> None:
    """按 schema v2 把复盘写入 Node 端 analysis_reports；仅 scheduler 触发时写库。

    任何持久化异常都只打日志、不向上抛，保证复盘主流程的返回值不受影响。
    """
    if state.get("trigger_source") != "scheduler":
        return
    try:
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
        content = _build_review_report(artifact)
        await node_api.save_analysis_report(
            report_type="review",
            report_date=report_date,
            content=content,
        )
    except Exception as e:
        logger.warning(
            "review_persist_failed",
            error=str(e),
            exc_info=True,
        )


# ============================================================================
# 主入口
# ============================================================================


async def run(state: AgentState) -> dict[str, object]:
    """收盘溯源：冻结事实 → 归档事实 → 单次 LLM 推理 → 校验 → 渲染 → 归档/缓存/持久化。

    严格顺序（未命中路径）：
    1. build_market_trace_snapshot(report_date) 冻结事实
    2. archive_market_trace_snapshot(snapshot) 归档不可变 facts.json
    3. get_deep_think().ainvoke → 解析 → 跨对象校验
    4. render_market_trace_markdown 渲染展示层
    5. archive_review(markdown, snapshot_id) 归档复盘报告（返回 bool）
    6. set_cached_review(report_date, artifact) 缓存完整工件（返回 bool）
    7. save_analysis_report 持久化到 DB

    缓存命中路径：除 ReviewArtifact.model_validate 外，还要重新执行
    validate_trace_against_snapshot 跨对象校验，并校验缓存日期与快照日期一致；
    任一不通过视为未命中，走完整路径。缓存命中时不请求 yfinance、财联社、
    Tavily 或 LLM。

    任一前置步骤失败都返回降级文本，不跳到后一步。
    """
    report_date = (
        state.get("report_date")
        or datetime.now().strftime("%Y-%m-%d")
    )

    # 1. 缓存检查（命中则校验工件 + 跨对象校验 + 日期一致、持久化、返回）
    cached = await get_cached_review(report_date)
    if cached is not None:
        artifact: ReviewArtifact | None = None
        try:
            artifact = ReviewArtifact.model_validate(cached)
        except Exception:
            logger.debug("cached_review_artifact_invalid", exc_info=True)
            artifact = None
        # 缓存命中后必须重新执行语义校验 + 校验缓存日期与快照日期一致，
        # 防止缓存里存了旧日期/非法语义的 artifact 被当作今日报告返回。
        if artifact is not None:
            try:
                if artifact.snapshot.trade_date != report_date:
                    raise ValueError(
                        f"cached snapshot trade_date {artifact.snapshot.trade_date} "
                        f"!= report_date {report_date}"
                    )
                validate_trace_against_snapshot(artifact.trace, artifact.snapshot)
            except Exception:
                logger.warning(
                    "cached_review_artifact_semantic_invalid",
                    report_date=report_date,
                    exc_info=True,
                )
                artifact = None
        if artifact is not None:
            await _persist_review_report(state, artifact)
            return {"final_response": artifact.markdown}
        # 缓存内容无效（如旧纯文本、日期不一致或语义非法），视为未命中，继续走完整路径

    # 2. 冻结事实快照（必须在 LLM 调用前）
    try:
        snapshot = await build_market_trace_snapshot(report_date)
    except Exception as e:
        logger.error(
            "review_snapshot_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 3. 归档不可变事实快照（在 LLM 推理前，保证事实先于展示层落盘）
    try:
        archive_market_trace_snapshot(snapshot)
    except Exception as e:
        logger.error(
            "review_archive_snapshot_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 4. 单次 LLM 调用 + 解析 + 跨对象校验
    try:
        llm = get_deep_think()
        snapshot_json = snapshot.model_dump_json(indent=2)
        messages = [
            SystemMessage(content=REVIEW_PROMPT),
            HumanMessage(content=snapshot_json),
        ]
        ai_message = await llm.ainvoke(messages)
        raw_text = (
            ai_message.content
            if isinstance(ai_message.content, str)
            else str(ai_message.content)
        )

        # 剥离代码围栏 + 解析 + 跨对象校验
        cleaned = _strip_code_fences(raw_text)
        trace = MarketTraceResult.model_validate_json(cleaned)
        validate_trace_against_snapshot(trace, snapshot)
    except Exception as e:
        logger.error(
            "review_trace_validation_failed",
            agent="review",
            error=str(e),
            exc_info=True,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 5. 渲染 Markdown + 构造 ReviewArtifact
    markdown = render_market_trace_markdown(trace, snapshot)
    artifact = ReviewArtifact(
        schema_version="1.0",
        snapshot=snapshot,
        trace=trace,
        markdown=markdown,
        trace_summary=_extract_trace_summary(markdown),
        sectors=_extract_review_sectors(markdown),
    )

    # 6. 归档复盘报告（仅在 facts.json 存在时创建 Markdown）
    #    严格失败顺序：归档失败 → 返回降级，不写 Redis / DB。
    if not archive_review(markdown, snapshot.snapshot_id):
        logger.error(
            "review_archive_review_failed",
            agent="review",
            snapshot_id=snapshot.snapshot_id,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 7. 缓存完整工件（model_dump(mode="json") 保证 JSON 可序列化）
    #    严格失败顺序：缓存失败 → 返回降级，不写 DB。
    if not await set_cached_review(report_date, artifact.model_dump(mode="json")):
        logger.error(
            "review_cache_set_failed",
            agent="review",
            report_date=report_date,
        )
        return {"final_response": DEGRADED_RESPONSE}

    # 8. 持久化到 DB（仅 scheduler 触发）
    await _persist_review_report(state, artifact)

    return {"final_response": markdown}
