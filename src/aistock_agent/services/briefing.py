"""由已持久化分析报告聚合的 Brief v1。"""

from __future__ import annotations

import json
from typing import Literal, Protocol

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.brief_contract import extract_controlled_brief_summary
from aistock_agent.utils.report_parser import extract_podcast_brief

BriefType = Literal["morning", "evening"]

_REQUIRED_TYPES: dict[BriefType, tuple[str, ...]] = {
    "morning": ("morning", "wind_leader", "hot_burst", "trend_score"),
    # 晚报三条均从 review 报告的不同部分提取（现象摘要/板块行情/主因链），
    # 在 build_brief 中特化处理，不遍历本元组。
    "evening": ("review",),
}
_TITLES = {
    "morning": "晨间市场展望",
    "wind_leader": "风口龙头",
    "hot_burst": "机构调研热点",
    "trend_score": "趋势股评分",
    "event_conduction": "事件传导分析",
    "review": "收盘复盘",
    "market_snapshot": "市场快照",
    "iterate": "迭代分析",
}
# 晚报各条目的标题（review 报告按展示维度拆分）
_ITEM_TITLES: dict[tuple[str, str], str] = {
    ("review", "sectors"): "市场快照",
    ("review", "attribution"): "归因结论",
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


def _content_conclusion(
    report_type: str, content: object, variant: str = "default"
) -> str | None:
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
    if report_type == "review" and variant == "attribution":
        return _review_attribution_conclusion(content)
    if report_type == "review" and variant == "sectors":
        return _review_sector_changes_conclusion(content)

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
    # details 是 schema 2.0 的合法字段，当 summary 和 podcast_brief 均空时读取
    if isinstance(display_report, dict):
        details = display_report.get("details")
        if isinstance(details, str) and details.strip():
            cleaned_details = details.strip()
            if not _looks_like_raw_json(cleaned_details):
                return cleaned_details[:200] if len(cleaned_details) > 200 else cleaned_details
    return None


def _review_attribution_conclusion(content: dict[str, object]) -> str | None:
    """从 review 报告的 market_trace.trace 提取主因链，拼成一句无冒号结论。

    只消费已验证的归因结果：attribution_status 为 confirmed/hypothesis 且
    存在主因候选时，把因果链节点（claim）去掉阶段标签与冒号，用逗号连成
    一句话总结（30-40 字，符合晚报结论展示规范）；其余情况返回"证据不足"
    降级文案。
    """
    market_trace = content.get("market_trace")
    if not isinstance(market_trace, dict):
        return None
    trace = market_trace.get("trace")
    if not isinstance(trace, dict):
        return None
    status = trace.get("attribution_status")
    candidates = trace.get("candidates")
    primary_id = trace.get("primary_chain_id")
    if status not in {"confirmed", "hypothesis"} or not isinstance(candidates, list):
        return "今日证据不足，未确认主因"
    primary: dict[str, object] | None = None
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate.get("id") == primary_id:
            primary = candidate
            break
    if primary is None:
        return "今日证据不足，未确认主因"
    chain = primary.get("chain")
    nodes = chain.get("nodes") if isinstance(chain, dict) else None
    claims: list[str] = []
    if isinstance(nodes, list) and nodes:
        for node in nodes:
            if not isinstance(node, dict):
                continue
            claim = node.get("claim")
            if isinstance(claim, str) and claim.strip():
                claims.append(claim.strip())
    if not claims:
        verdict = primary.get("verdict")
        return (
            verdict
            if isinstance(verdict, str) and verdict.strip()
            else "今日证据不足，未确认主因"
        )
    # 去掉"触发：/传导：/结果："式冒号与阶段标签，连成一句话；
    # confirmed 加"主因是"前缀、hypothesis 用"或受…等因素影响"句式，凑足 30-40 字
    if status == "confirmed":
        return f"今日市场主因是{'，'.join(claims)}"
    return f"今日市场可能受{'、'.join(claims)}等因素影响"


def _review_sector_changes_conclusion(content: dict[str, object]) -> str | None:
    """从 review 报告的 market_trace.snapshot.a_share.sectors 提取领涨/领跌板块。

    sectors 结构（Node 收盘/腾讯快照）：
      {"top_gainers": [{"name", "pct_change", ...}, ...],
       "top_losers":  [{"name", "pct_change", ...}, ...]}
    拼成一句无冒号的行情快照（30-40 字，符合晚报结论展示规范）。
    """
    market_trace = content.get("market_trace")
    if not isinstance(market_trace, dict):
        return None
    snapshot = market_trace.get("snapshot")
    if not isinstance(snapshot, dict):
        return None
    a_share = snapshot.get("a_share")
    if not isinstance(a_share, dict):
        return None
    sectors = a_share.get("sectors")
    if not isinstance(sectors, dict):
        return None

    def _format_sectors(items: object, verb: str, limit: int = 2) -> list[str]:
        parts: list[str] = []
        if not isinstance(items, list):
            return parts
        for item in items:
            if len(parts) >= limit:
                break
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            pct = item.get("pct_change")
            if (
                isinstance(name, str)
                and name.strip()
                and isinstance(pct, int | float)
                and not isinstance(pct, bool)
            ):
                parts.append(f"{name.strip()}{verb}{abs(pct):.2f}%")
        return parts

    gainers = _format_sectors(sectors.get("top_gainers"), "涨")
    losers = _format_sectors(sectors.get("top_losers"), "跌")
    if not gainers and not losers:
        return None
    if gainers and losers:
        return f"今日{'、'.join(gainers)}，{'、'.join(losers)}"
    if gainers:
        return f"今日{'、'.join(gainers)}，无显著领跌板块"
    return f"今日{'、'.join(losers)}，无显著领涨板块"


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


def _to_item(
    report_type: str, report: dict[str, object], variant: str = "default"
) -> dict[str, object] | None:
    report_id = report.get("id")
    persisted_type = report.get("report_type")
    status = report.get("status")
    created_at = _created_at(report)
    conclusion = _content_conclusion(report_type, report.get("content"), variant)
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
        "title": _ITEM_TITLES.get((report_type, variant), _TITLES.get(report_type, report_type)),
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
    items: list[dict[str, object]] = []
    missing_sources: list[str] = []

    if brief_type == "evening":
        # 晚报三条均来自 review 报告的不同展示维度，
        # 归因结论（主因链）作为头条放在第一条：
        #   归因结论 / 市场快照（板块行情）/ 收盘复盘（现象摘要）
        review_report = await api.get_analysis_report("review", report_date)
        if review_report is None:
            missing_sources.append("review")
        else:
            for variant in ("attribution", "sectors", "summary"):
                item = _to_item("review", review_report, variant=variant)
                if item is None:
                    missing_sources.append(f"review.{variant}")
                else:
                    items.append(item)
    else:
        for report_type in _REQUIRED_TYPES["morning"]:
            report = await api.get_analysis_report(report_type, report_date)
            item = _to_item(report_type, report) if report else None
            if item is None:
                missing_sources.append(report_type)
            else:
                items.append(item)

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
