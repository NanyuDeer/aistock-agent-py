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
    MarketTraceResult,
    MarketTraceSnapshot,
)
from aistock_agent.services.archiver import archive_review
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

    - primary_chain_id 必须指向 status="supported" 的候选，不得为 null
    - alternative_chain_id 必须指向不同的 supported 或 weak 候选；null 允许
    - primary 与 alternative 不得相同
    """
    candidate_by_id = {c.id: c for c in trace.candidates}

    if trace.primary_chain_id is None:
        raise ValueError("primary_chain_id must not be null")

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
    """
    candidate_by_id = {c.id: c for c in trace.candidates}

    primary = (
        candidate_by_id.get(trace.primary_chain_id)
        if trace.primary_chain_id
        else None
    )
    if primary is None or primary.chain is None:
        raise ValueError("primary candidate has no chain")
    primary_stages = [n.stage for n in primary.chain.nodes]
    if primary_stages != REQUIRED_CHAIN_STAGES:
        raise ValueError(
            f"primary chain stages mismatch: {primary_stages} != {REQUIRED_CHAIN_STAGES}"
        )

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
    """跨对象校验：候选完整性、source_id 存在性、chain 选择与阶段。"""
    categories = {candidate.category for candidate in trace.candidates}
    if (
        len(trace.candidates) != 4
        or categories != REQUIRED_CANDIDATE_CATEGORIES
        or {candidate.id for candidate in trace.candidates} != categories
    ):
        raise ValueError("candidate categories are incomplete")
    ids = set(snapshot.sources)
    for candidate in trace.candidates:
        for source_id in (
            candidate.supporting_evidence_ids
            + candidate.counter_evidence_ids
            + [item for node in (candidate.chain.nodes if candidate.chain else []) for item in node.evidence_ids]  # noqa: E501
        ):
            if source_id not in ids:
                raise ValueError(f"unknown source_id: {source_id}")
    validate_selected_chain_ids(trace)
    validate_chain_stages(trace)


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


def _format_source_time(source) -> str:
    """格式化 SourceRecord.occurred_at 为可读时间。"""
    if source.occurred_at is None:
        return "未知时间"
    try:
        return source.occurred_at.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return str(source.occurred_at)


def _render_candidate(candidate, indent: str = "") -> list[str]:
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
        lines.append("- 未选定主因。")
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
# 持久化辅助（保留：schema v2，Task 4 不动，Task 5 会精炼）
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


def _build_review_report(markdown: str) -> dict[str, object]:
    """把 LLM 产出的 markdown 封装成 schema v2 的持久化结构。

    schema v2：display_report 提供前端直接消费的字段，details 保留原始 markdown。
    不触发任何 LLM 调用——所有摘要/板块均通过正则从 markdown 中提取。

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

    return {
        "display_report": {
            "summary": summary,
            "details": markdown,
            "stocks": [],
            "sectors": _extract_review_sectors(markdown),
            "risks": [],
        },
        "podcast_brief": "",
        "schema_version": "2.0",
    }


async def _persist_review_report(state: AgentState, markdown: str) -> None:
    """按 schema v2 把复盘写入 Node 端 analysis_reports；仅 scheduler 触发时写库。

    任何持久化异常都只打日志、不向上抛，保证复盘主流程的返回值不受影响。
    """
    if state.get("trigger_source") != "scheduler":
        return
    try:
        report_date = state.get("report_date") or datetime.now().strftime("%Y-%m-%d")
        content = _build_review_report(markdown)
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
    """收盘溯源：冻结事实 → 单次 LLM 推理 → 校验 → 渲染 → 缓存/持久化。

    流程：
    1. 缓存命中 → 直接返回（scheduler 触发时仍持久化）
    2. build_market_trace_snapshot(report_date) 冻结事实
    3. get_deep_think().ainvoke([SystemMessage, HumanMessage(snapshot_json)])
    4. 剥离代码围栏 → MarketTraceResult.model_validate_json
    5. validate_trace_against_snapshot 跨对象校验
    6. 校验失败 → 返回降级文本，不写缓存
    7. 校验通过 → render_market_trace_markdown → 缓存 + 归档 + 持久化
    """
    report_date = (
        state.get("report_date")
        or datetime.now().strftime("%Y-%m-%d")
    )

    # 1. 缓存检查（命中则直接返回）
    cached = await get_cached_review()
    if cached:
        await _persist_review_report(state, cached)
        return {"final_response": cached}

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

    # 3-5. 单次 LLM 调用 + 解析 + 跨对象校验
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

    # 6-7. 渲染 Markdown + 缓存 + 归档 + 持久化（校验通过才写缓存）
    markdown = render_market_trace_markdown(trace, snapshot)
    await set_cached_review(markdown)
    archive_review(markdown)
    await _persist_review_report(state, markdown)

    return {"final_response": markdown}
