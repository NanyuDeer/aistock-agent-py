"""报告优先的 AI 投顾节点。

用户对话只消费已持久化的分析报告。没有可追溯报告时，返回明确的
降级状态，不调用实时工具补造事实。
"""

from __future__ import annotations

import re

import structlog
from langchain_core.messages import SystemMessage

from aistock_agent.prompts.workers.ai_advisor import AI_ADVISOR_PROMPT
from aistock_agent.services.data_client import node_api
from aistock_agent.services.llm import get_deep_think, get_quick_think
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import shanghai_today
from aistock_agent.utils.message import extract_last_human_message
from aistock_agent.utils.report_parser import extract_display_report

logger = structlog.get_logger()

# 意图映射到已持久化报告的真实 report_type。event 是用户意图别名，
# event_conduction 才是持久化报告类型。
INTENT_REPORT_MAP: dict[str, tuple[str, ...]] = {
    "morning": ("morning",),
    "wind_leader": ("wind_leader",),
    "hot_burst": ("hot_burst",),
    "stock": ("stock",),
    "sector": ("wind_leader",),
    "event": ("event_conduction",),
    "alert": ("alert",),
    "review": ("review",),
    "trend_score": ("trend_score",),
    "general": ("morning", "wind_leader", "hot_burst"),
}

_INTENT_KEYWORDS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("morning", ("晨报", "早报", "盘前", "开盘前")),
    ("wind_leader", ("风口", "板块", "行业", "龙头")),
    ("hot_burst", ("机构调研", "热门股", "调研热股", "机构票", "共振")),
    ("stock", ("个股", "股票")),
    ("event", ("事件", "传导")),
    ("alert", ("异动", "预警")),
    ("review", ("复盘", "晚报", "收盘")),
    ("trend_score", ("趋势评分", "趋势股", "趋势分析", "K线趋势")),
)

_REPORT_LABELS: dict[str, str] = {
    "morning": "晨报",
    "wind_leader": "风口",
    "hot_burst": "热门股",
    "stock": "个股",
    "sector": "板块",
    "event": "事件传导",
    "alert": "异动预警",
    "review": "收盘复盘",
    "trend_score": "趋势评分",
    "general": "综合投顾",
}

_MAX_SUBQUESTIONS = 3

# StockTraceArtifact 尚未落地前，stock 子意图缺失来源必须指向 stock_trace，
# 不能暗示已经支持可追溯个股结论。其他 report_type 缺失时仍用自身名称。
_MISSING_SOURCE_NAMES: dict[str, str] = {
    "stock": "stock_trace",
}

_STOCK_TRACE_PENDING_MESSAGE = (
    "个股可追溯结论（stock_trace）尚未落地，暂不支持实时个股报告；"
    "请等待 StockTraceArtifact 上线后再查询。"
)

_ADVISOR_TRACE_SCHEMA_VERSION = "advisor_trace.v1"


def _stock_trace_degradation() -> dict[str, object]:
    """stock_trace 尚未落地，stock 子意图只能返回固定降级结构。"""
    return {
        "intent": "stock",
        "reports": [],
        "sources": [],
        "as_of": None,
        "missing_sources": ["stock_trace"],
        "degraded": True,
    }


def _advisor_trace(subquestions: list[dict[str, object]]) -> dict[str, object]:
    """汇总子问题的可追溯状态，供所有传输通道原样透传。"""
    missing_sources: list[str] = []
    for subquestion in subquestions:
        sources = subquestion.get("missing_sources")
        if not isinstance(sources, list):
            continue
        for source in sources:
            if isinstance(source, str) and source not in missing_sources:
                missing_sources.append(source)
    return {
        "schema_version": _ADVISOR_TRACE_SCHEMA_VERSION,
        "subquestions": subquestions,
        "missing_sources": missing_sources,
        "degraded": any(bool(subquestion.get("degraded")) for subquestion in subquestions),
    }


def split_subquestion_intents(user_message: str, primary_intent: str) -> list[str]:
    """从原始用户请求轻量拆解最多三个可映射的子问题。

    这是投顾节点内的确定性拆解，不引入通用 QA Router 或额外状态机。
    有明确关键词时按其在消息中的出现顺序取前 3 个；单意图请求则保留
    supervisor 已识别的旧入口意图。
    """
    matches: list[tuple[int, int, str]] = []
    for priority, (intent, keywords) in enumerate(_INTENT_KEYWORDS):
        for keyword in keywords:
            for match in re.finditer(re.escape(keyword), user_message):
                matches.append((match.start(), priority, intent))

    stock_match = re.search(r"\b\d{6}\b", user_message)
    if stock_match:
        matches.append((stock_match.start(), 99, "stock"))

    intents: list[str] = []
    for _, _, intent in sorted(matches):
        if intent not in intents:
            intents.append(intent)

    normalized_primary = primary_intent if primary_intent in INTENT_REPORT_MAP else "general"
    if not intents:
        return [normalized_primary]
    if normalized_primary == "stock" and "stock" not in intents:
        intents.append("stock")
    if "stock" in intents and intents.index("stock") >= _MAX_SUBQUESTIONS:
        return [*intents[:_MAX_SUBQUESTIONS - 1], "stock"]
    return intents[:_MAX_SUBQUESTIONS]


def _report_as_of(report: dict[str, object]) -> str | None:
    """只使用持久化报告记录的真实截至时间。"""
    created_at = report.get("created_at")
    if isinstance(created_at, str) and created_at:
        return created_at
    return None


def _report_text(report_type: str, content: dict[str, object]) -> str:
    """读取报告的可展示结论；事件仅使用其真实嵌套播报摘要。"""
    if report_type == "event_conduction":
        analysis_reports = content.get("analysis_reports")
        if not isinstance(analysis_reports, dict):
            return ""
        event_brief = analysis_reports.get("event_podcast_brief")
        return event_brief.strip() if isinstance(event_brief, str) else ""
    return extract_display_report(content)


def _report_record(
    report: dict[str, object],
    expected_type: str,
    symbol: str | None = None,
) -> dict[str, object] | None:
    """将一条真实持久化行规范为投顾可追溯记录。"""
    report_id = report.get("id")
    actual_type = report.get("report_type")
    status = report.get("status")
    data_source = report.get("data_source")
    content = report.get("content")
    as_of = _report_as_of(report)
    has_report_id = (
        isinstance(report_id, str) and bool(report_id.strip())
    ) or (isinstance(report_id, int) and not isinstance(report_id, bool) and report_id > 0)
    if (
        not has_report_id
        or actual_type != expected_type
        or status != "completed"
        or not isinstance(data_source, str)
        or not data_source.strip()
        or not as_of
        or not isinstance(content, dict)
    ):
        return None

    if expected_type in {"stock", "alert"}:
        if not symbol or content.get("symbol") != symbol:
            return None

    text = _report_text(expected_type, content)
    if not text:
        return None

    return {
        "report_type": expected_type,
        "text": text,
        "source": {
            "id": str(report_id),
            "type": expected_type,
            "source": data_source,
            "as_of": as_of,
        },
        "as_of": as_of,
    }


async def _read_persisted_reports(
    report_type: str,
    report_date: str,
    symbol: str | None,
) -> list[dict[str, object]]:
    """按真实报告类型读取持久化工件，事件走专用日期列表接口。"""
    try:
        if report_type == "event_conduction":
            list_reports = getattr(node_api, "list_analysis_reports", None)
            if not callable(list_reports):
                logger.warning("advisor_event_report_list_unavailable")
                return []
            reports = await list_reports(report_type, report_date)
            return [report for report in reports if isinstance(report, dict)]

        if report_type in {"stock", "alert"} and not symbol:
            return []

        report = await node_api.get_analysis_report(
            report_type,
            report_date,
            user_id=symbol if report_type in {"stock", "alert"} else None,
        )
        return [report] if isinstance(report, dict) else []
    except Exception as exc:
        logger.warning(
            "advisor_report_fetch_failed",
            report_type=report_type,
            error=str(exc),
        )
        return []


async def fetch_subquestion_reports(
    intent: str,
    report_date: str,
    user_id: str | None,
    symbol: str | None = None,
) -> dict[str, object]:
    """按意图映射读取报告并返回可追溯的子问题结果。"""
    normalized_intent = intent if intent in INTENT_REPORT_MAP else "general"
    if normalized_intent == "stock":
        return _stock_trace_degradation()

    records: list[dict[str, object]] = []
    missing_sources: list[str] = []

    for report_type in INTENT_REPORT_MAP[normalized_intent]:
        persisted_reports = await _read_persisted_reports(report_type, report_date, symbol)
        valid_records = [
            record
            for report in persisted_reports
            if (record := _report_record(report, report_type, symbol)) is not None
        ]
        if valid_records:
            records.extend(valid_records)
        else:
            # stock 缺失时声明 stock_trace（StockTraceArtifact 尚未落地），
            # 不能暗示已经支持可追溯个股结论。
            missing_sources.append(_MISSING_SOURCE_NAMES.get(report_type, report_type))

    sources = [record["source"] for record in records]
    as_of_values = [record["as_of"] for record in records if isinstance(record["as_of"], str)]
    return {
        "intent": normalized_intent,
        "reports": records,
        "sources": sources,
        "as_of": max(as_of_values) if as_of_values else None,
        "missing_sources": missing_sources,
        "degraded": bool(missing_sources),
    }


async def _fetch_relevant_reports(intent: str, report_date: str) -> dict[str, str]:
    """兼容旧单意图调用，返回按真实报告类型索引的报告文本。"""
    result = await fetch_subquestion_reports(intent, report_date, None)
    reports = result["reports"]
    if not isinstance(reports, list):
        return {}
    return {
        str(record["report_type"]): str(record["text"])
        for record in reports
        if isinstance(record, dict)
        and isinstance(record.get("report_type"), str)
        and isinstance(record.get("text"), str)
    }


def _format_available_reports(subquestions: list[dict[str, object]]) -> str:
    """将已持久化报告文本整理为 LLM 的唯一事实输入。"""
    parts: list[str] = []
    for subquestion in subquestions:
        reports = subquestion.get("reports")
        if not isinstance(reports, list):
            continue
        for report in reports:
            if not isinstance(report, dict):
                continue
            report_type = report.get("report_type")
            text = report.get("text")
            if not isinstance(report_type, str) or not isinstance(text, str):
                continue
            label = _REPORT_LABELS.get(report_type, report_type)
            parts.append(f"### {label}\n{text}")
    return "\n\n".join(parts)


def _format_trace(subquestions: list[dict[str, object]]) -> str:
    """以稳定文本格式公开每个子问题的来源和降级状态。"""
    lines: list[str] = []
    for subquestion in subquestions:
        intent = subquestion.get("intent")
        intent_name = intent if isinstance(intent, str) else "general"
        label = _REPORT_LABELS.get(intent_name, intent_name)
        sources = subquestion.get("sources")
        source_text: list[str] = []
        if isinstance(sources, list):
            for source in sources:
                if not isinstance(source, dict):
                    continue
                report_type = source.get("type")
                report_id = source.get("id")
                data_source = source.get("source")
                if isinstance(report_type, str) and isinstance(report_id, str):
                    source_name = (
                        data_source
                        if isinstance(data_source, str) and data_source
                        else "数据源未记录"
                    )
                    source_text.append(f"{report_type}#{report_id}（{source_name}）")
        missing_sources = subquestion.get("missing_sources")
        missing_text = ""
        if isinstance(missing_sources, list):
            missing_text = ", ".join(
                item for item in missing_sources if isinstance(item, str)
            )
        as_of = subquestion.get("as_of")
        as_of_text = as_of if isinstance(as_of, str) and as_of else "未记录"
        status = "降级" if subquestion.get("degraded") else "正常"
        line = (
            f"• {label}：来源：{'；'.join(source_text) if source_text else '无'}；"
            f"截至：{as_of_text}；状态：{status}"
        )
        if missing_text:
            line += f"；缺失来源：{missing_text}"
        lines.append(line)
    return "\n".join(lines)


def _subquestion_degraded_message(intent_name: str) -> str:
    """子问题缺失报告时的降级文案。

    stock 子意图在 StockTraceArtifact 落地前必须明确指向 stock_trace，
    不能暗示已经支持可追溯个股结论；其他意图沿用通用降级文案。
    """
    if intent_name == "stock":
        return _STOCK_TRACE_PENDING_MESSAGE
    return "未找到可追溯的已持久化报告，无法生成实时结论。"


async def _run_from_persisted_reports(state: AgentState) -> dict[str, object]:
    """以最多三个子问题读取已持久化报告并返回带来源的汇总。"""
    intent = state.get("intent", "general") or "general"
    raw_report_date = state.get("report_date")
    report_date = (
        raw_report_date
        if isinstance(raw_report_date, str) and raw_report_date
        else shanghai_today().isoformat()
    )
    user_message = extract_last_human_message(state.get("messages", []))
    intents = split_subquestion_intents(user_message, intent)

    subquestions = [
        await fetch_subquestion_reports(
            subquestion_intent,
            report_date,
            state.get("user_id"),
            state.get("symbol"),
        )
        for subquestion_intent in intents
    ]
    has_reports = any(bool(subquestion.get("reports")) for subquestion in subquestions)
    trace = _advisor_trace(subquestions)

    if not has_reports:
        # 所有子问题都无报告：逐项给出降级文案（stock 指向 stock_trace），不调用 LLM。
        parts: list[str] = []
        for subquestion in subquestions:
            intent_name = subquestion.get("intent")
            intent_key = intent_name if isinstance(intent_name, str) else "general"
            label = _REPORT_LABELS.get(intent_key, "综合投顾")
            parts.append(f"### {label}\n{_subquestion_degraded_message(intent_key)}")
        combined = "\n\n".join(parts)
        return {
            "final_response": f"{combined}\n\n【报告追溯】\n{_format_trace(subquestions)}",
            "advisor_trace": trace,
        }

    summaries: list[str] = []
    for subquestion in subquestions:
        intent_name = subquestion.get("intent")
        intent_key = intent_name if isinstance(intent_name, str) else "general"
        label = _REPORT_LABELS.get(intent_key, "综合投顾")
        if not subquestion.get("reports"):
            summaries.append(f"### {label}\n{_subquestion_degraded_message(intent_key)}")
            continue

        prompt = AI_ADVISOR_PROMPT.replace(
            "{{AVAILABLE_REPORTS}}",
            _format_available_reports([subquestion]),
        )
        try:
            summary = await _stream_llm_with_fallback(prompt)
        except Exception as exc:
            logger.warning("advisor_summary_failed", intent=intent_name, error=str(exc))
            summary = "已读取可追溯的已持久化报告，模型汇总暂不可用，请参考以下来源。"
            subquestion["degraded"] = True
            trace = _advisor_trace(subquestions)
        summaries.append(f"### {label}\n{summary}")

    logger.info(
        "advisor_reports_fetched",
        intents=intents,
        report_date=report_date,
        degraded=trace["degraded"],
    )
    combined_summary = "\n\n".join(summaries)
    return {
        "final_response": f"{combined_summary}\n\n【报告追溯】\n{_format_trace(subquestions)}",
        "advisor_trace": trace,
    }


async def run(state: AgentState) -> dict[str, object]:
    """以最多三个子问题读取已持久化报告，并在节点异常时明确降级。"""
    try:
        return await _run_from_persisted_reports(state)
    except Exception as exc:
        logger.error("agent_run_failed", agent="ai_advisor", error=str(exc), exc_info=True)
        return {
            "final_response": "智能投顾暂时不可用，请稍后重试",
            "advisor_trace": {
                "schema_version": _ADVISOR_TRACE_SCHEMA_VERSION,
                "subquestions": [],
                "missing_sources": ["advisor_unavailable"],
                "degraded": True,
            },
        }


async def _stream_llm_with_fallback(prompt: str) -> str:
    """仅基于传入持久化报告做语言整理，模型失败则交给调用方降级。"""
    messages = [SystemMessage(content=prompt)]
    for llm_factory, label in ((get_deep_think, "deep"), (get_quick_think, "quick")):
        try:
            llm = llm_factory()
            response_chunks: list[str] = []
            async for chunk in llm.astream(messages):
                if chunk.content:
                    response_chunks.append(
                        chunk.content if isinstance(chunk.content, str) else str(chunk.content)
                    )
            response = "".join(response_chunks).strip()
            if response:
                return response
            raise RuntimeError("LLM 未返回投顾汇总")
        except Exception as exc:
            logger.warning("advisor_llm_failed", tier=label, error=str(exc))
            if label == "quick":
                raise
    raise RuntimeError("LLM 未返回投顾汇总")
