"""由已持久化分析报告聚合的 Brief v1。"""

from __future__ import annotations

import json
from typing import Literal, Protocol

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.brief_contract import extract_controlled_brief_summary
from aistock_agent.utils.report_parser import extract_podcast_brief

BriefType = Literal["morning", "evening"]

_REQUIRED_TYPES: dict[BriefType, tuple[str, ...]] = {
    "morning": ("morning", "wind_leader", "hot_burst"),
    "evening": ("review", "market_snapshot", "iterate"),
}
_TITLES = {
    "morning": "晨间市场展望",
    "wind_leader": "长期风口与龙头",
    "hot_burst": "机构调研热点",
    "event_conduction": "事件传导分析",
    "review": "收盘复盘",
    "market_snapshot": "市场快照",
    "iterate": "迭代分析",
}


class BriefingClient(Protocol):
    async def get_analysis_report(
        self, report_type: str, report_date: str, user_id: str | None = None
    ) -> dict[str, object] | None: ...

    async def list_analysis_reports(
        self, report_type: str, report_date: str
    ) -> list[dict[str, object]]: ...

    async def save_analysis_report(
        self,
        *,
        report_type: str,
        report_date: str,
        content: object,
        user_id: str | None = None,
        data_source: str | None = None,
        status: str = "completed",
        generation_time_ms: int | None = None,
        model_version: str | None = None,
        error_message: str | None = None,
    ) -> dict[str, object] | None: ...


def _content_conclusion(report_type: str, content: object) -> str | None:
    if not isinstance(content, dict):
        return None
    if report_type in {"market_snapshot", "iterate"}:
        return extract_controlled_brief_summary(report_type, content.get("brief_summary"))
    if report_type == "event_conduction":
        analysis_reports = content.get("analysis_reports")
        if not isinstance(analysis_reports, dict):
            return None
        event_brief = analysis_reports.get("event_podcast_brief")
        if not isinstance(event_brief, str):
            return None
        cleaned_event_brief = event_brief.strip()
        return None if _looks_like_raw_json(cleaned_event_brief) else cleaned_event_brief or None

    display_report = content.get("display_report")
    if isinstance(display_report, dict):
        summary = display_report.get("summary")
        if isinstance(summary, str) and summary.strip():
            cleaned_summary = summary.strip()
            if not _looks_like_raw_json(cleaned_summary):
                return cleaned_summary
    podcast = extract_podcast_brief(content)
    if podcast:
        cleaned_podcast = podcast.strip()
        if not _looks_like_raw_json(cleaned_podcast):
            return cleaned_podcast
    return None


def _looks_like_raw_json(text: str) -> bool:
    """识别原始 JSON/repr/大对象：以 { 或 [ 开头且可被 json.loads 解析。

    Brief conclusion 只接受可读、受控的结论文本。当上游误把完整 JSON
    （如 market_snapshot/iterate 的原始 payload）写入 text 字段时，
    本函数返回 True，调用方据此返回 None，使 Brief 进入降级路径并声明
    缺失来源，而不是把原始 JSON 当作结论展示给用户。
    """
    if not text:
        return False
    stripped = text.lstrip()
    if not (stripped.startswith("{") or stripped.startswith("[")):
        return False
    try:
        json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _created_at(report: dict[str, object]) -> str | None:
    created_at = report.get("created_at")
    return created_at if isinstance(created_at, str) and created_at else None


def _is_valid_report_id(report_id: object) -> bool:
    return (isinstance(report_id, int) and not isinstance(report_id, bool) and report_id > 0) or (
        isinstance(report_id, str) and bool(report_id.strip())
    )


def _to_item(report_type: str, report: dict[str, object]) -> dict[str, object] | None:
    report_id = report.get("id")
    persisted_type = report.get("report_type")
    status = report.get("status")
    created_at = _created_at(report)
    conclusion = _content_conclusion(report_type, report.get("content"))
    source = report.get("data_source")
    if (
        not _is_valid_report_id(report_id)
        or persisted_type != report_type
        or status != "completed"
        or not created_at
        or not conclusion
        or not isinstance(source, str)
        or not source.strip()
    ):
        return None

    return {
        "title": _TITLES.get(report_type, report_type),
        "conclusion": conclusion,
        "evidence": [{
            "report_type": report_type,
            "id": str(report_id),
            "data_source": source,
            "created_at": created_at,
        }],
        "as_of": created_at,
        "confidence": "unknown",
        "uncertainty": "upstream confidence unavailable",
    }


async def build_brief(
    brief_type: BriefType,
    report_date: str,
    *,
    api: BriefingClient = node_api,
) -> dict[str, object]:
    """仅从指定交易日的持久化报告构造可追溯 Brief。"""
    required_types = _REQUIRED_TYPES[brief_type]
    items: list[dict[str, object]] = []
    missing_sources: list[str] = []

    for report_type in required_types:
        report = await api.get_analysis_report(report_type, report_date)
        item = _to_item(report_type, report) if report else None
        if item is None:
            missing_sources.append(report_type)
        else:
            items.append(item)

    if brief_type == "morning":
        events = await api.list_analysis_reports("event_conduction", report_date)
        ordered_events = sorted(
            events,
            key=lambda report: _created_at(report) or "",
            reverse=True,
        )
        for report in ordered_events:
            item = _to_item("event_conduction", report)
            if item is not None:
                items.append(item)
                if len(items) >= 5:
                    break

    return {
        "schema_version": "brief.v1",
        "brief_type": brief_type,
        "as_of": f"{report_date}T00:00:00+08:00",
        "items": items,
        "degraded": bool(missing_sources) or len(items) < 3,
        "missing_sources": missing_sources,
    }


async def build_and_persist_brief(
    brief_type: BriefType,
    report_date: str,
    *,
    api: BriefingClient = node_api,
) -> bool:
    """聚合并持久化 Brief，供公开读取与播报消费。"""
    brief = await build_brief(brief_type, report_date, api=api)
    saved = await api.save_analysis_report(
        report_type=f"brief_{brief_type}",
        report_date=report_date,
        content=brief,
        data_source="brief_aggregator",
        status="completed",
    )
    return saved is not None
