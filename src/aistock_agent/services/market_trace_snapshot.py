"""市场溯源事实快照构建服务 — 冻结事实、来源与现象发现。

本模块只做事实冻结和确定性现象发现，不调用 LLM，不输出因果判断。
因果归因由后续 Task 4 的 review agent 在 JSON 契约约束下完成。

设计要点：
- ``collect_global_market_facts`` 从 ``market_tools`` 导入，使 mock 路径
  ``aistock_agent.services.market_trace_snapshot.collect_global_market_facts``
  能拦截本模块内的本地引用。
- ``TavilyService`` 同理，mock 路径
  ``aistock_agent.services.market_trace_snapshot.TavilyService.search`` 可拦截。
- ``build_market_trace_snapshot`` 不 import 任何 Agent 模块，不调用 LLM。
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Literal

import structlog

from aistock_agent.schemas.market_trace import (
    DataAvailability,
    MarketTraceSnapshot,
    SourceCollectionStatus,
    SourceRecord,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_store import EventRecord
from aistock_agent.services.morning_forecast_extractor import extract_morning_forecast
from aistock_agent.services.phenomenon_discovery import discover_market_phenomenon
from aistock_agent.services.tavily import TavilyService
from aistock_agent.tools.market_tools import collect_global_market_facts

logger = structlog.get_logger()


class MarketTraceSnapshotUnavailable(Exception):  # noqa: N818
    """当日 A 股收盘事实快照不可用。

    Node 返回 None、status 非 complete 或 coverage 不完整时抛出。
    不产出部分快照，由上层返回降级文本。

    命名保留 brief 中定义的 ``MarketTraceSnapshotUnavailable``（不带 Error
    后缀），与 Node.js 侧 ``MarketSnapshotUnavailableError`` 对应但非同一类。
    """


# ============================================================================
# 辅助函数
# ============================================================================


def _parse_yyyymmdd(date_str: object) -> datetime | None:
    """将 YYYYMMDD 字符串解析为 UTC datetime。"""
    if not isinstance(date_str, str) or len(date_str) != 8:
        return None
    try:
        return datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime | None:
    """从多种字符串格式解析 datetime，无时区时假设 UTC。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _parse_news_datetime(value: object) -> datetime | None:
    """解析财联社/新闻时间字段。

    与 :func:`_parse_datetime` 的区别（为什么不能复用）：
    财联社 telegraph 的 ``time`` 是**上海无时区**格式 ``"2026-08-05 14:30:00"``，
    若按 UTC 解析会比真实 UTC 晚 8 小时，导致 ``occurred_at > captured_at``
    被误判为未来数据而全部跳过（晚报 cls_news 失效根因之一）。

    - unix 秒（telegraph 的 ``timestamp`` 字段）→ UTC datetime
    - ISO 字符串：带时区直接解析；无时区按 Asia/Shanghai（UTC+8）解析
    """
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(float(value), tz=UTC)
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str) or not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        # 财联社 time 为上海时钟；假设 UTC+8
        dt = dt.replace(tzinfo=timezone(timedelta(hours=8)))
    return dt


def _safe_str(value: object, default: str = "") -> str:
    """安全转换为 str。"""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


def _safe_optional_str(value: object) -> str | None:
    """安全转换为 ``str | None``。

    配合 mypy 类型收窄：直接 ``item.get("url") if isinstance(item.get("url"), str) else None``
    会让 mypy 推断为 ``object | None``（因为两次 ``get`` 调用结果分别判定），
    通过本函数一次性收窄，避免 ``type: ignore``。
    """
    if not isinstance(value, str):
        return None
    return value.strip() or None


def _has_numeric_fields(record: dict[str, object], fields: tuple[str, ...]) -> bool:
    """仅当一组聚合事实的关键数值全部真实存在时返回 True。"""
    return all(
        isinstance(record.get(field), int | float) and not isinstance(record.get(field), bool)
        for field in fields
    )


def _has_any_numeric_field(record: dict[str, object], fields: tuple[str, ...]) -> bool:
    """至少一个关键字段包含真实数值时返回 True。"""
    return any(
        isinstance(record.get(field), int | float) and not isinstance(record.get(field), bool)
        for field in fields
    )


def _sector_item_count(record: dict[str, object], field: str) -> int:
    items = record.get(field)
    if not isinstance(items, list):
        return 0
    return sum(isinstance(item, dict) and bool(item) for item in items)


def _append_missing(missing_fields: list[str], field: str) -> None:
    if field not in missing_fields:
        missing_fields.append(field)


def _log_telegraph_response(
    telegraph_data: object, report_date: str, *, snapshot_kind: str
) -> None:
    """记录 telegraph 接口返回的条目数，便于定位 cls_news 缺失根因。

    telegraph 接口返回 ``{date, items, total, degraded}``（app-api 侧），
    也可能返回 ``{code, data}`` 包装（node_api 透传）。兼容两种结构。
    """
    if not isinstance(telegraph_data, dict):
        logger.info(
            "cls_telegraph_response",
            snapshot_kind=snapshot_kind,
            report_date=report_date,
            item_count=0,
            raw_type=type(telegraph_data).__name__,
        )
        return
    items = telegraph_data.get("items")
    if not isinstance(items, list) and isinstance(telegraph_data.get("data"), dict):
        items = telegraph_data["data"].get("items")
    item_count = len(items) if isinstance(items, list) else 0
    logger.info(
        "cls_telegraph_response",
        snapshot_kind=snapshot_kind,
        report_date=report_date,
        item_count=item_count,
        total=telegraph_data.get("total") if isinstance(telegraph_data, dict) else None,
        degraded=telegraph_data.get("degraded") if isinstance(telegraph_data, dict) else None,
    )


def _quick_availability(close_data: dict[str, object]) -> dict[str, DataAvailability]:
    """Parse Node quick availability without trusting malformed metadata."""
    raw_availability = close_data.get("quick_data_availability")
    if not isinstance(raw_availability, dict):
        return {}

    availability: dict[str, DataAvailability] = {}
    for field, raw_value in raw_availability.items():
        if not isinstance(field, str) or not isinstance(raw_value, dict):
            continue
        try:
            value = dict(raw_value)
            if "reason" in value:
                value["reason"] = _safe_availability_reason(value.get("reason"))
            availability[field] = DataAvailability.model_validate(value)
        except ValueError:
            availability[field] = DataAvailability(
                state="unavailable", reason="invalid_availability_metadata"
            )
    return availability


_SAFE_AVAILABILITY_REASONS = {
    "prior_day_amount_unavailable",
    "limit_pool_unavailable",
    "moneyflow_ths_unavailable",
    "provider_empty",
    "availability_not_declared",
    "invalid_availability_metadata",
}


def _safe_availability_reason(value: object) -> str:
    if isinstance(value, str) and value in _SAFE_AVAILABILITY_REASONS:
        return value
    return "provider_reported_unavailable"


def _fact_is_usable(availability: DataAvailability | None) -> bool:
    return availability is None or availability.state in {"available", "partial"}


def _availability_allows_fact(availability: DataAvailability | None, is_quick: bool) -> bool:
    if is_quick:
        return availability is not None and _fact_is_usable(availability)
    return _fact_is_usable(availability)


def _finalize_fact_availability(
    data_availability: dict[str, DataAvailability],
    field: str,
    usable: bool,
    missing_fields: list[str],
    missing_field: str,
) -> None:
    if usable:
        data_availability.setdefault(field, DataAvailability(state="available"))
        return
    if data_availability.get(field) is None or data_availability[field].state != "unavailable":
        data_availability[field] = DataAvailability(
            state="unavailable", reason="aggregate_fact_invalid_or_missing"
        )
    _append_missing(missing_fields, missing_field)


def _normalize_date_yyyymmdd(value: object) -> str | None:
    """把 YYYYMMDD 或 YYYY-MM-DD 字符串规范化为 ``YYYY-MM-DD``，无效时返回 None。

    Node 端 ``trade_date`` 可能是 ``20260719`` 或 ``2026-07-19``；
    本函数统一为 ``YYYY-MM-DD`` 形式，便于与 ``report_date`` 比较。
    """
    if not isinstance(value, str):
        return None
    s = value.strip()
    if len(s) == 8 and s.isdigit():
        return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"
    if (
        len(s) == 10
        and s[0:4].isdigit()
        and s[4] == "-"
        and s[5:7].isdigit()
        and s[7] == "-"
        and s[8:10].isdigit()
    ):
        return s
    return None


_TUSHARE_INDEX_CODE_RE = re.compile(r"^(\d{6})\.(SH|SZ)$", re.IGNORECASE)
_TENCENT_INDEX_CODE_RE = re.compile(r"^(sh|sz)(\d{6})$", re.IGNORECASE)


def _parse_index_ts_code(value: object) -> tuple[str, str] | None:
    if not isinstance(value, str):
        return None
    if match := _TUSHARE_INDEX_CODE_RE.fullmatch(value):
        code, exchange = match.groups()
        return code, exchange.upper()
    if match := _TENCENT_INDEX_CODE_RE.fullmatch(value):
        exchange, code = match.groups()
        return code, exchange.upper()
    return None


def normalize_a_share(close_data: dict[str, object]) -> dict[str, object]:
    """复制并规范化 Node A 股快照，不修改调用方持有的原始对象。"""
    normalized = dict(close_data)
    normalized_indexes: dict[str, dict[str, object]] = {}
    raw_indexes = close_data.get("indexes")
    if isinstance(raw_indexes, list):
        for raw_index in raw_indexes:
            if not isinstance(raw_index, dict):
                continue
            ts_code = raw_index.get("ts_code")
            parsed_code = _parse_index_ts_code(ts_code)
            if parsed_code is None:
                continue
            code, exchange = parsed_code
            normalized_index = dict(raw_index)
            normalized_index["ts_code"] = f"{code}.{exchange}"
            normalized_index["change_pct"] = normalized_index.pop(
                "pct_chg", normalized_index.get("change_pct")
            )
            normalized_index["source_id"] = f"INDEX_{code}_{exchange}"
            normalized_indexes[f"{exchange}{code}"] = normalized_index
    normalized["indexes"] = normalized_indexes
    return normalized


# ============================================================================
# 快照构建
# ============================================================================


# ============================================================================
# 归一化辅助函数（从 build_market_trace_snapshot 内联逻辑提取，
# 供 build_market_trace_snapshot 和 build_quick_snapshot 共用）
# ============================================================================


def _normalize_index_facts(
    normalized_a_share: dict[str, object],
    sources: dict[str, SourceRecord],
    missing_fields: list[str],
    trade_date_dt: datetime,
    captured_at: datetime,
) -> None:
    """A 股指数事实 → SourceRecord"""
    indexes_map = normalized_a_share.get("indexes")
    if isinstance(indexes_map, dict):
        for idx in indexes_map.values():
            if not isinstance(idx, dict):
                continue
            ts_code = _safe_str(idx.get("ts_code"))
            if not ts_code:
                continue
            source_id = f"INDEX_{ts_code.replace('.', '_')}"
            sources[source_id] = SourceRecord(
                source_id=source_id,
                kind="market_fact",
                provider=_safe_str(idx.get("source"), "tushare:index_daily"),
                title=_safe_str(idx.get("name"), ts_code),
                content=(
                    f"trade_date={idx.get('trade_date')}, "
                    f"close={idx.get('close')}, "
                    f"change_pct={idx.get('change_pct')}, "
                    f"amount={idx.get('amount')}"
                ),
                url=None,
                occurred_at=trade_date_dt,
                captured_at=captured_at,
                source_level="market_data",
            )


def _normalize_aggregate_facts(
    normalized_a_share: dict[str, object],
    sources: dict[str, SourceRecord],
    missing_fields: list[str],
    trade_date_dt: datetime,
    captured_at: datetime,
    data_availability: dict[str, DataAvailability],
    *,
    is_quick: bool,
) -> None:
    """A 股聚合事实（广度/成交额/涨跌停/主力资金/板块）→ SourceRecord"""
    # 市场广度
    breadth_dict = normalized_a_share.get("breadth")
    breadth_availability = data_availability.get("breadth")
    breadth_fields = (
        "total_count",
        "advance_count",
        "decline_count",
        "flat_count",
        "advance_ratio",
    )
    valid_breadth = (
        _availability_allows_fact(breadth_availability, is_quick)
        and isinstance(breadth_dict, dict)
        and _has_numeric_fields(breadth_dict, breadth_fields)
        and all(
            math.isfinite(float(breadth_dict[field]))
            and float(breadth_dict[field]).is_integer()
            and float(breadth_dict[field]) >= 0
            for field in ("total_count", "advance_count", "decline_count", "flat_count")
        )
        and float(breadth_dict["total_count"]) > 0
        and float(breadth_dict["advance_count"]) <= float(breadth_dict["total_count"])
        and float(breadth_dict["decline_count"]) <= float(breadth_dict["total_count"])
        and float(breadth_dict["flat_count"]) <= float(breadth_dict["total_count"])
        and math.isfinite(float(breadth_dict["advance_ratio"]))
        and 0 <= float(breadth_dict["advance_ratio"]) <= 1
        and float(breadth_dict["advance_count"])
        + float(breadth_dict["decline_count"])
        + float(breadth_dict["flat_count"])
        == float(breadth_dict["total_count"])
        and math.isclose(
            float(breadth_dict["advance_ratio"]),
            float(breadth_dict["advance_count"]) / float(breadth_dict["total_count"]),
            rel_tol=1e-6,
            abs_tol=1e-6,
        )
    )
    if valid_breadth and isinstance(breadth_dict, dict):
        sources["BREADTH_ALL"] = SourceRecord(
            source_id="BREADTH_ALL",
            kind="market_fact",
            provider=_safe_str(breadth_dict.get("source"), "tushare:daily"),
            title="全市场涨跌家数",
            content=(
                f"total={breadth_dict.get('total_count')}, "
                f"advance={breadth_dict.get('advance_count')}, "
                f"decline={breadth_dict.get('decline_count')}, "
                f"flat={breadth_dict.get('flat_count')}, "
                f"advance_ratio={breadth_dict.get('advance_ratio')}"
            ),
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )
    _finalize_fact_availability(
        data_availability,
        "breadth",
        valid_breadth,
        missing_fields,
        "a_share.breadth",
    )

    # 成交额
    turnover_dict = normalized_a_share.get("turnover")
    turnover_availability = data_availability.get("turnover")
    turnover_fields = (
        set(turnover_availability.available_fields)
        if turnover_availability is not None and turnover_availability.state == "partial"
        else {"amount_yuan", "previous_amount_yuan", "change_pct"}
    )
    valid_turnover = (
        _availability_allows_fact(turnover_availability, is_quick)
        and isinstance(turnover_dict, dict)
        and any(
            field in turnover_fields and _has_numeric_fields(turnover_dict, (field,))
            for field in ("amount_yuan", "previous_amount_yuan", "change_pct")
        )
    )
    if valid_turnover and isinstance(turnover_dict, dict):
        turnover_content = ", ".join(
            f"{field}={turnover_dict.get(field)}"
            for field in ("amount_yuan", "previous_amount_yuan", "change_pct")
            if field in turnover_fields and _has_numeric_fields(turnover_dict, (field,))
        )
        sources["TURNOVER_ALL"] = SourceRecord(
            source_id="TURNOVER_ALL",
            kind="market_fact",
            provider=_safe_str(turnover_dict.get("source"), "tushare:daily"),
            title="全市场成交额",
            content=turnover_content,
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )
    _finalize_fact_availability(
        data_availability,
        "turnover",
        valid_turnover,
        missing_fields,
        "a_share.turnover",
    )

    # 涨跌停与连板
    limits_dict = normalized_a_share.get("limits")
    limits_availability = data_availability.get("limits")
    limit_fields = (
        set(limits_availability.available_fields)
        if limits_availability is not None and limits_availability.state == "partial"
        else {"up_count", "down_count", "broken_count", "highest_board"}
    )
    valid_limits = (
        _availability_allows_fact(limits_availability, is_quick)
        and isinstance(limits_dict, dict)
        and any(
            field in limit_fields and _has_numeric_fields(limits_dict, (field,))
            for field in ("up_count", "down_count", "broken_count", "highest_board")
        )
    )
    if valid_limits and isinstance(limits_dict, dict):
        limit_content = ", ".join(
            f"{field}={limits_dict.get(field)}"
            for field in ("up_count", "down_count", "broken_count", "highest_board")
            if field in limit_fields and _has_numeric_fields(limits_dict, (field,))
        )
        if limits_availability is not None and limits_availability.approximate:
            limit_content += ", approximate=true"
        sources["LIMITS_ALL"] = SourceRecord(
            source_id="LIMITS_ALL",
            kind="market_fact",
            provider="tushare:limit_list_ths",
            title="涨跌停与连板统计",
            content=limit_content,
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )
    _finalize_fact_availability(
        data_availability,
        "limits",
        valid_limits,
        missing_fields,
        "a_share.limits",
    )

    # 主力资金
    main_force_dict = normalized_a_share.get("main_force")
    main_force_availability = data_availability.get("main_force")
    valid_main_force = (
        _availability_allows_fact(main_force_availability, is_quick)
        and isinstance(main_force_dict, dict)
        and (
            main_force_availability is None
            or main_force_availability.state != "partial"
            or "large_and_extra_large_net_yuan" in main_force_availability.available_fields
        )
        and _has_numeric_fields(
            main_force_dict,
            ("large_and_extra_large_net_yuan",),
        )
    )
    if not valid_main_force:
        # 诊断日志：定位 main_force 缺失的具体原因（quick 快照下 Tushare 数据未就绪属预期）
        logger.info(
            "main_force_invalid",
            is_quick=is_quick,
            availability_state=main_force_availability.state if main_force_availability else None,
            availability_reason=main_force_availability.reason if main_force_availability else None,
            has_dict=isinstance(main_force_dict, dict),
            value=main_force_dict.get("large_and_extra_large_net_yuan")
            if isinstance(main_force_dict, dict)
            else None,
        )
    if valid_main_force and isinstance(main_force_dict, dict):
        sources["MAIN_FORCE_ALL"] = SourceRecord(
            source_id="MAIN_FORCE_ALL",
            kind="market_fact",
            provider=_safe_str(main_force_dict.get("source"), "tushare:moneyflow_ths"),
            title="大单加特大单净额",
            content=(
                f"large_and_extra_large_net_yuan="
                f"{main_force_dict.get('large_and_extra_large_net_yuan')}"
            ),
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )
    _finalize_fact_availability(
        data_availability,
        "main_force",
        valid_main_force,
        missing_fields,
        "a_share.main_force.large_and_extra_large_net_yuan",
    )

    # 板块
    sectors_dict = normalized_a_share.get("sectors")
    sector_fields = ("top_gainers", "top_losers", "top_inflows", "top_outflows")
    sectors_availability = data_availability.get("sectors")
    usable_sector_fields = (
        set(sectors_availability.available_fields)
        if sectors_availability is not None and sectors_availability.state == "partial"
        else set(sector_fields)
    )
    valid_sectors = (
        _availability_allows_fact(sectors_availability, is_quick)
        and isinstance(sectors_dict, dict)
        and any(
            field in usable_sector_fields and _sector_item_count(sectors_dict, field) > 0
            for field in sector_fields
        )
    )
    if valid_sectors and isinstance(sectors_dict, dict):
        sector_content = ", ".join(
            f"{field}_count={_sector_item_count(sectors_dict, field)}"
            for field in sector_fields
            if field in usable_sector_fields
        )
        sources["SECTORS_ALL"] = SourceRecord(
            source_id="SECTORS_ALL",
            kind="market_fact",
            provider="tushare:moneyflow_cnt_ths",
            title="概念板块涨跌与资金流排序",
            content=sector_content,
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )
    _finalize_fact_availability(
        data_availability,
        "sectors",
        valid_sectors,
        missing_fields,
        "a_share.sectors",
    )


def _normalize_global_facts(
    global_facts: list[dict[str, object]],
    sources: dict[str, SourceRecord],
    missing_fields: list[str],
    captured_at: datetime,
    fetch_error: Exception | None = None,
) -> SourceCollectionStatus:
    """境外行情事实 → SourceRecord"""
    global_counter = 0
    for fact in global_facts:
        if not isinstance(fact, dict):
            continue
        global_counter += 1
        source_id = f"GLOBAL_{global_counter:03d}"
        occurred_at = _parse_datetime(fact.get("observed_at"))
        sources[source_id] = SourceRecord(
            source_id=source_id,
            kind="market_fact",
            provider="tencent:quote",
            title=_safe_str(fact.get("name"), _safe_str(fact.get("ticker"))),
            content=(
                f"ticker={fact.get('ticker')}, "
                f"price={fact.get('price')}, "
                f"change_pct={fact.get('change_pct')}, "
                f"observed_at={fact.get('observed_at')}"
            ),
            url=None,
            occurred_at=occurred_at,
            captured_at=captured_at,
            source_level="market_data",
        )
    if fetch_error is not None:
        _append_missing(missing_fields, "global_markets")
        return SourceCollectionStatus(
            state="unavailable",
            provider="tencent:quote",
            reason=type(fetch_error).__name__,
        )
    if global_counter == 0:
        _append_missing(missing_fields, "global_markets")
        return SourceCollectionStatus(
            state="empty",
            provider="tencent:quote",
            reason="provider_returned_no_items",
        )
    return SourceCollectionStatus(
        state="available", provider="tencent:quote", item_count=global_counter
    )


def _normalize_news_facts(
    news_data: dict[str, object] | None,
    sources: dict[str, SourceRecord],
    missing_fields: list[str],
    captured_at: datetime,
    fetch_error: Exception | None = None,
    source_kind: str = "latest",
) -> SourceCollectionStatus:
    """财联社快讯 → SourceRecord（event_evidence）

    支持两种数据结构（由 ``source_kind`` 标记，归一化时统一处理）：
    - ``telegraph``: ``{items: [{id, title, content, time, timestamp}]}``（无 link 字段）
    - ``latest``: ``{items: [{id, link, title, time, content}]}``（有 link 字段）

    occurred_at 统一用 ``time`` 字段解析；URL 优先取 ``link``（latest 流），
    兼容旧 ``url`` 字段；telegraph 流无 URL 字段时为 None。
    """
    news_items: list[dict[str, object]] = []
    if isinstance(news_data, dict):
        raw_items = news_data.get("items", news_data.get("news", []))
        if isinstance(raw_items, list):
            news_items = [item for item in raw_items if isinstance(item, dict)]

    news_counter = 0
    causal_ready_count = 0
    skipped_future = 0  # occurred_at > captured_at（未来数据）
    skipped_no_time = 0  # occurred_at 解析失败
    for item in news_items:
        # 时间：telegraph 优先用 timestamp（unix 秒）；否则 time（上海无时区，按 UTC+8 解析）
        raw_ts = item.get("timestamp")
        occurred_at = _parse_news_datetime(raw_ts) if raw_ts is not None else None
        if occurred_at is None:
            occurred_at = _parse_news_datetime(item.get("time", item.get("ctime", "")))
        if occurred_at is not None and occurred_at > captured_at:
            skipped_future += 1
            continue
        if occurred_at is None:
            skipped_no_time += 1
            continue
        # URL：latest 用 link 字段，telegraph 无 URL；同时兼容旧 url 字段。
        # telegraph 无 URL 时用财联社详情页兜底，保证可溯源（否则被判 invalid_for_causality）。
        url = _safe_optional_str(item.get("link")) or _safe_optional_str(item.get("url"))
        if not url:
            item_id = item.get("id")
            if isinstance(item_id, str | int) and str(item_id).strip().isdigit():
                url = f"https://www.cls.cn/detail/{item_id}"
        news_counter += 1
        source_id = f"NEWS_{news_counter:03d}"
        sources[source_id] = SourceRecord(
            source_id=source_id,
            kind="event_evidence",
            provider="cls",
            title=_safe_str(item.get("title"), "无标题"),
            content=_safe_str(item.get("brief", item.get("content", "")))[:500],
            url=url,
            occurred_at=occurred_at,
            captured_at=captured_at,
            source_level="reporting",
        )
        if url:
            causal_ready_count += 1
    if fetch_error is not None:
        _append_missing(missing_fields, "cls_news")
        logger.warning(
            "cls_news_missing_fetch_error",
            source_kind=source_kind,
            raw_item_count=len(news_items),
            error_class=type(fetch_error).__name__,
        )
        return SourceCollectionStatus(
            state="unavailable",
            provider="cls",
            reason=type(fetch_error).__name__,
        )
    if not news_items:
        _append_missing(missing_fields, "cls_news")
        logger.warning(
            "cls_news_missing_empty",
            source_kind=source_kind,
            raw_item_count=0,
        )
        return SourceCollectionStatus(
            state="empty", provider="cls", reason="provider_returned_no_items"
        )
    if causal_ready_count == 0:
        _append_missing(missing_fields, "cls_news")
        logger.warning(
            "cls_news_missing_invalid_for_causality",
            source_kind=source_kind,
            raw_item_count=len(news_items),
            kept_count=news_counter,
            skipped_future=skipped_future,
            skipped_no_time=skipped_no_time,
            causal_ready_count=0,
        )
        return SourceCollectionStatus(
            state="invalid_for_causality",
            provider="cls",
            item_count=len(news_items),
            reason="items_missing_url_or_occurred_at",
        )
    logger.info(
        "cls_news_available",
        source_kind=source_kind,
        raw_item_count=len(news_items),
        kept_count=news_counter,
        skipped_future=skipped_future,
        skipped_no_time=skipped_no_time,
        causal_ready_count=causal_ready_count,
    )
    return SourceCollectionStatus(state="available", provider="cls", item_count=news_counter)


def _map_event_store_source_level(level: str) -> Literal["primary", "reporting"]:
    """事件库 source_level（A/B/C/D）→ review SourceRecord 档位。

    事件库 A 级（官方/一手来源）→ primary；B/C/D → reporting。
    review 的 ``SourceRecord.source_level`` 是
    ``Literal[primary/reporting/market_data]``，与事件库 A/B/C/D 不是同一套
    枚举——简报 Step 3 的"原样透传 source_level"在 Pydantic Literal 校验下
    不可行（Task 7 记录偏差）。
    """
    return "primary" if level == "A" else "reporting"


def _normalize_event_store_facts(
    events: list[EventRecord],
    sources: dict[str, SourceRecord],
    missing_fields: list[str],
    captured_at: datetime,
) -> SourceCollectionStatus:
    """统一事件库 → SourceRecord（event_evidence）

    大盘溯源证据源优先读事件库（统一事件抓取中台，2026-08-12）：事件库有
    当日数据时直接用事件库做 news_facts（读库优先），缺库才走原
    telegraph/latest 直采。

    字段映射（简报 Step 3）：source_id=EVENT_{event_id[:19]}（日期+hash 前 8 位，
    可读且可追溯；不用序号——评审 Minor 1，事件库 event_id 可溯源）、
    kind=event_evidence、provider=source、title、content=summary[:500]（空则
    title）、url、occurred_at=scrape_at（解析失败兜底 captured_at）、
    source_level 映射。
    """
    if not events:
        _append_missing(missing_fields, "cls_news")
        logger.warning("event_store_missing_empty", item_count=0)
        return SourceCollectionStatus(
            state="empty", provider="event_store", reason="provider_returned_no_items"
        )
    event_counter = 0
    causal_ready_count = 0
    for ev in events:
        event_counter += 1
        # event_id 形如 "{score_date}-{content_hash[:16]}"（event_store.normalize_event）；
        # [:8] 只含日期段（"2026-07-"）同日全碰撞会互相覆盖，[:19] 保留日期 + hash 前 8 位，
        # 兼顾可读与可追溯且唯一（Task 7 Fix Round Minor 1 偏差，见报告）。
        source_id = f"EVENT_{ev['event_id'][:19]}"
        summary = ev["summary"]
        url = _safe_optional_str(ev["url"])
        occurred_at = _parse_news_datetime(ev["scrape_at"])
        if occurred_at is None:
            occurred_at = captured_at
        sources[source_id] = SourceRecord(
            source_id=source_id,
            kind="event_evidence",
            provider=_safe_str(ev["source"], "event_store"),
            title=_safe_str(ev["title"], "无标题"),
            content=summary[:500] if summary else _safe_str(ev["title"], "无标题"),
            url=url,
            occurred_at=occurred_at,
            captured_at=captured_at,
            source_level=_map_event_store_source_level(ev["source_level"]),
        )
        if url:
            causal_ready_count += 1
    if causal_ready_count == 0:
        # 事件非空但全部无 URL：与 _normalize_news_facts 语义对齐（评审 Important 1）。
        # 事件库 occurred_at 必兜底 captured_at（非 None），故仅 URL 决定因果就绪。
        # 防御性补强：正常数据事件库事件均有 URL（cls 无 URL 时 normalize 已兜底详情页），
        # 该分支当前实际不可达，但状态语义必须与直采路径一致，避免误报 available。
        _append_missing(missing_fields, "cls_news")
        logger.warning(
            "event_store_missing_invalid_for_causality",
            item_count=event_counter,
            causal_ready_count=0,
        )
        return SourceCollectionStatus(
            state="invalid_for_causality",
            provider="event_store",
            item_count=event_counter,
            reason="items_missing_url",
        )
    logger.info(
        "review_event_store_used",
        item_count=event_counter,
        causal_ready_count=causal_ready_count,
    )
    return SourceCollectionStatus(
        state="available", provider="event_store", item_count=event_counter
    )


def _normalize_search_facts(
    tavily_result_1: dict[str, object],
    tavily_result_2: dict[str, object],
    sources: dict[str, SourceRecord],
    missing_fields: list[str],
    captured_at: datetime,
    fetch_errors: tuple[Exception | None, Exception | None] = (None, None),
) -> dict[str, SourceCollectionStatus]:
    """Tavily 检索结果 → SourceRecord（event_evidence）"""
    search_counter = 0
    statuses: dict[str, SourceCollectionStatus] = {}
    for label, status_key, tavily_result, fetch_error in [
        ("tavily_search_1", "tavily_domestic_policy", tavily_result_1, fetch_errors[0]),
        ("tavily_search_2", "tavily_global_risk", tavily_result_2, fetch_errors[1]),
    ]:
        results = tavily_result.get("results") if isinstance(tavily_result, dict) else None
        if fetch_error is not None:
            _append_missing(missing_fields, label)
            statuses[status_key] = SourceCollectionStatus(
                state="unavailable",
                provider="tavily",
                reason=type(fetch_error).__name__,
            )
            continue
        if not isinstance(results, list) or not results:
            _append_missing(missing_fields, label)
            statuses[status_key] = SourceCollectionStatus(
                state="empty",
                provider="tavily",
                reason="provider_returned_no_items",
            )
            continue
        source_count = 0
        causal_ready_count = 0
        for item in results:
            if not isinstance(item, dict):
                continue
            pub_date = item.get("published_date", item.get("publishedDate", ""))
            occurred_at = _parse_datetime(pub_date)
            # 未来数据防呆：published_date 有效且晚于捕获时刻才跳过
            if occurred_at is not None and occurred_at > captured_at:
                continue
            # Tavily 结果常缺 published_date（2026-08 实测无该字段）；
            # 不丢弃，用捕获时刻兜底（仅要求 URL 可溯源）。
            if occurred_at is None:
                occurred_at = captured_at
            url = _safe_optional_str(item.get("url"))
            search_counter += 1
            source_count += 1
            source_id = f"SEARCH_{search_counter:03d}"
            sources[source_id] = SourceRecord(
                source_id=source_id,
                kind="event_evidence",
                provider="tavily",
                title=_safe_str(item.get("title"), "无标题"),
                content=_safe_str(item.get("content", ""))[:500],
                url=url,
                occurred_at=occurred_at,
                captured_at=captured_at,
                source_level="reporting",
            )
            if url:
                causal_ready_count += 1
        if causal_ready_count == 0:
            _append_missing(missing_fields, label)
            statuses[status_key] = SourceCollectionStatus(
                state="invalid_for_causality",
                provider="tavily",
                item_count=len(results),
                reason="items_missing_url",
            )
        else:
            statuses[status_key] = SourceCollectionStatus(
                state="available", provider="tavily", item_count=source_count
            )
    return statuses


async def build_market_trace_snapshot(report_date: str) -> MarketTraceSnapshot:
    """构建市场溯源事实快照。

    顺序固定（brief Step 5）：
    1. 获取 Node 收盘快照；没有 status=complete 时立即失败。
    2. 以同一个 captured_at 收集境外行情、财联社快讯和两组固定 Tavily 检索。
    3. 将所有输入归一化为 SourceRecord，递增 source_id，不可用项写入 missing_fields。
    4. 只用规范化 a_share、真实 sources 和缺失字段运行确定性 discovery。
    5. 返回事实快照；不调用 LLM，不输出因果判断。
    """
    captured_at = datetime.now(UTC)

    # ── 1. 获取 Node 收盘快照（带 last-close 降级） ──
    # 先尝试 close-snapshot（要求 >= 15:30）；若不可用（盘中/凌晨），
    # 降级到 last-close-snapshot（返回最近一个已完成交易日数据）。
    close_data = await node_api.get("/internal/market/close-snapshot")
    used_last_close = False
    if close_data is None:
        close_data = await node_api.get_last_close_snapshot()
        if close_data is not None:
            used_last_close = True
            logger.info(
                "build_snapshot_fell_back_to_last_close",
                report_date=report_date,
                trade_date=close_data.get("trade_date"),
            )
    if close_data is None:
        raise MarketTraceSnapshotUnavailable(
            "Node close-snapshot returned None (market not closed or service unavailable)"
        )
    if close_data.get("status") != "complete":
        raise MarketTraceSnapshotUnavailable(
            f"Node close-snapshot status is not complete: {close_data.get('status')}"
        )
    coverage = close_data.get("coverage")
    coverage_dict = coverage if isinstance(coverage, dict) else {}
    current_daily = coverage_dict.get("current_daily")
    current_daily_dict = current_daily if isinstance(current_daily, dict) else {}
    if current_daily_dict.get("complete") is not True:
        raise MarketTraceSnapshotUnavailable(
            "Node close-snapshot coverage.current_daily.complete is not True"
        )
    # 同时校验 previous_daily.complete — 防止 Node 把"今日已收盘"伪装成 complete、
    # 但 previous_daily 仍滞后的场景；当日 facts 必须与 previous_daily 共同完整。
    previous_daily = coverage_dict.get("previous_daily")
    previous_daily_dict = previous_daily if isinstance(previous_daily, dict) else {}
    if previous_daily_dict.get("complete") is not True:
        raise MarketTraceSnapshotUnavailable(
            "Node close-snapshot coverage.previous_daily.complete is not True"
        )

    # ── 2. 校验 Node trade_date 与 report_date 一致 ──
    # 必须在 collect_global_market_facts、node 新闻接口和 Tavily 调用之前完成，
    # 不一致时立即抛 MarketTraceSnapshotUnavailable，避免浪费外部 API 配额。
    # 场景：周末/节假日调用时 Node 没有当日数据，trade_date 仍是上一交易日；
    # 不能把旧事实写入新日期快照。
    # 例外：used_last_close=True 时 trade_date 是最近交易日而非 report_date，
    # 此时 snapshot 以 Node 返回的 trade_date 为准。
    trade_date_node = _safe_str(close_data.get("trade_date"))
    trade_date_normalized = _normalize_date_yyyymmdd(trade_date_node)
    if trade_date_normalized is None:
        raise MarketTraceSnapshotUnavailable(
            f"Node close-snapshot trade_date is not a valid YYYYMMDD/YYYY-MM-DD: "
            f"{trade_date_node!r}"
        )
    trade_date_dt = _parse_yyyymmdd(trade_date_normalized.replace("-", ""))
    if trade_date_dt is None:
        raise MarketTraceSnapshotUnavailable(
            f"Node close-snapshot trade_date is not a valid calendar date: {trade_date_node!r}"
        )
    if not used_last_close and trade_date_normalized != report_date:
        raise MarketTraceSnapshotUnavailable(
            f"Node close-snapshot trade_date {trade_date_normalized} != report_date "
            f"{report_date}; refusing to write stale facts into a new-date snapshot"
        )

    normalized_a_share = normalize_a_share(close_data)

    # ── 3. 收集外部来源（同一 captured_at）──
    # 只有 status/coverage/date 三重校验全部通过，才允许调用 yfinance、财联社、Tavily。

    # 境外行情（异步函数，腾讯行情源经 app-api 聚合）
    global_fetch_error: Exception | None = None
    try:
        global_facts = await collect_global_market_facts(captured_at)
    except Exception as e:
        logger.warning("collect_global_market_facts_failed", error_class=type(e).__name__)
        global_facts = []
        global_fetch_error = e

    # 财联社当日全量电报（优先），降级到最新快讯
    # 电报接口返回当日全量快讯（含 timestamp 字段），适合溯源；
    # 失败时降级到 latest（仅最近若干条，含 link 字段）。
    # ── 统一事件抓取中台：证据源优先读事件库，缺库降级到直采（2026-08-12）──
    # 事件库（report_type=event_scrape）有当日数据时，用事件库事实做
    # news_facts（读库优先）；空/读失败（load_event_scrape 内部已吞异常返回
    # []）时，完整回到原 telegraph/latest 直采（缺库降级，P0 功能保护）。
    from aistock_agent.services.event_store import load_event_scrape  # noqa: PLC0415

    # 防御：load_event_scrape 内部已吞异常返回 []，此处再兜一层——
    # 即使契约被违反（未预期异常），也降级到原直采，保证 P0 功能保护。
    try:
        event_store_events = await load_event_scrape(report_date)
    except Exception as e:  # noqa: BLE001
        logger.warning("event_store_read_failed_fallback_to_direct", error_class=type(e).__name__)
        event_store_events = []
    news_data = None
    news_fetch_error: Exception | None = None
    news_source_kind: str = "telegraph"  # 标记数据来源，供归一化区分字段差异
    if event_store_events:
        logger.info("review_event_store_used", count=len(event_store_events))
        news_source_kind = "event_store"
    else:
        # 缺库降级：走原 telegraph/latest 直采
        try:
            telegraph_data = await node_api.get(
                f"/internal/news/telegraph?date={report_date}&limit=200"
            )
            if telegraph_data is not None:
                news_data = telegraph_data
                news_source_kind = "telegraph"
                _log_telegraph_response(telegraph_data, report_date, snapshot_kind="full")
        except Exception as e:
            logger.warning("cls_telegraph_fetch_failed", error_class=type(e).__name__)
            news_fetch_error = e

        # 降级：电报接口失败或返回 None 时回退到最新快讯
        if news_data is None:
            try:
                news_data = await node_api.get("/internal/news/latest")
                news_source_kind = "latest"
                # 电报失败但 latest 成功，清除电报阶段的错误标记，
                # 避免 _normalize_news_facts 误判为 unavailable。
                news_fetch_error = None
                logger.info("cls_telegraph_fallback_to_latest", report_date=report_date)
            except Exception as e:
                logger.warning("cls_news_fetch_failed", error_class=type(e).__name__)
                news_fetch_error = e

    # 两组固定 Tavily 检索
    tavily_query_1 = f"{report_date} 中国 资本市场 政策 产业 公告"
    tavily_query_2 = f"{report_date} 全球股市 利率 汇率 大宗商品 地缘风险"
    tavily_error_1: Exception | None = None
    tavily_error_2: Exception | None = None
    try:
        tavily_result_1 = TavilyService.search(query=tavily_query_1, topic="news", max_results=5)
    except Exception as e:
        logger.warning("tavily_search_1_failed", error_class=type(e).__name__)
        tavily_result_1 = {}
        tavily_error_1 = e
    try:
        tavily_result_2 = TavilyService.search(query=tavily_query_2, topic="news", max_results=5)
    except Exception as e:
        logger.warning("tavily_search_2_failed", error_class=type(e).__name__)
        tavily_result_2 = {}
        tavily_error_2 = e

    # ── 4. 归一化为 SourceRecord ──
    sources: dict[str, SourceRecord] = {}
    missing_fields: list[str] = []
    data_availability: dict[str, DataAvailability] = {}

    # ── 3.5. 读取当日晨报预测（失败不阻断）──
    # 放在 missing_fields 初始化后，便于失败/缺失时直接写入 missing_fields。
    # extract_morning_forecast 内部已处理缓存/Node 读取/LLM 提取的异常并返回 None，
    # 这里再兜一层 try 防止未预期异常阻断 snapshot 构建。
    morning_forecast = None
    try:
        morning_forecast = await extract_morning_forecast(report_date)
    except Exception as e:
        logger.warning("morning_forecast_inject_failed", error_class=type(e).__name__)
    if morning_forecast is None:
        _append_missing(missing_fields, "morning_forecast")

    _normalize_index_facts(normalized_a_share, sources, missing_fields, trade_date_dt, captured_at)
    _normalize_aggregate_facts(
        normalized_a_share,
        sources,
        missing_fields,
        trade_date_dt,
        captured_at,
        data_availability,
        is_quick=False,
    )
    if news_source_kind == "event_store":
        cls_news_status = _normalize_event_store_facts(
            event_store_events, sources, missing_fields, captured_at
        )
    else:
        cls_news_status = _normalize_news_facts(
            news_data,
            sources,
            missing_fields,
            captured_at,
            news_fetch_error,
            source_kind=news_source_kind,
        )
    collection_status = {
        "global_markets": _normalize_global_facts(
            global_facts, sources, missing_fields, captured_at, global_fetch_error
        ),
        "cls_news": cls_news_status,
    }
    collection_status.update(
        _normalize_search_facts(
            tavily_result_1,
            tavily_result_2,
            sources,
            missing_fields,
            captured_at,
            (tavily_error_1, tavily_error_2),
        )
    )

    # ── 5. 在冻结事实和真实来源上确定性发现市场现象 ──
    discovery = discover_market_phenomenon(
        normalized_a_share,
        sources,
        captured_at,
        missing_fields,
    )

    # ── 5. 返回事实快照 ──
    # snapshot_id 包含 captured_at 时间戳（微秒精度），支持同日失败后的安全重试：
    # 同一 captured_at 的重试产生相同 snapshot_id → facts 文件不可覆盖（FileExistsError），
    # 不同 captured_at 的重试产生不同 snapshot_id → 允许新建 facts 文件，不阻断后续重试。
    trade_date_yyyymmdd = trade_date_node.replace("-", "")
    captured_at_suffix = captured_at.strftime("%Y%m%dT%H%M%S%fZ")
    snapshot_id = f"trace-{trade_date_yyyymmdd}-{captured_at_suffix}"

    return MarketTraceSnapshot(
        snapshot_id=snapshot_id,
        trade_date=report_date,
        captured_at=captured_at,
        a_share=normalized_a_share,
        sources=sources,
        missing_fields=missing_fields,
        phenomenon_discovery=discovery,
        data_availability=data_availability,
        collection_status=collection_status,
        morning_forecast=morning_forecast,
    )


# ============================================================================
# Quick Snapshot（15:30 腾讯实时行情）
# ============================================================================


async def build_quick_snapshot(report_date: str) -> MarketTraceSnapshot:
    """构建 quick 版市场溯源事实快照（15:30 腾讯实时行情）。

    与 build_market_trace_snapshot 的区别：
    - 调用 /internal/market/quick-snapshot（腾讯行情）而非
      /internal/market/close-snapshot（Tushare）
    - 不校验 coverage.current_daily.complete（quick 版 coverage 不完整是正常的）
    - 不校验 previous_daily（quick 版无前日数据）
    - 其余归一化、discovery 逻辑与 full 版一致

    Raises:
        MarketTraceSnapshotUnavailable: Node quick-snapshot 不可用或 trade_date 不匹配。
    """
    captured_at = datetime.now(UTC)

    # ── 1. 获取 Node quick snapshot ──
    close_data = await node_api.get_quick_snapshot()
    if close_data is None:
        raise MarketTraceSnapshotUnavailable(
            "Node quick-snapshot returned None (market not closed or service unavailable)"
        )
    if close_data.get("status") != "complete":
        raise MarketTraceSnapshotUnavailable(
            f"Node quick-snapshot status is not complete: {close_data.get('status')}"
        )

    # ── 2. 校验 trade_date ──
    trade_date_node = _safe_str(close_data.get("trade_date"))
    trade_date_normalized = _normalize_date_yyyymmdd(trade_date_node)
    if trade_date_normalized is None:
        raise MarketTraceSnapshotUnavailable(
            f"Node quick-snapshot trade_date invalid: {trade_date_node!r}"
        )
    trade_date_dt = _parse_yyyymmdd(trade_date_normalized.replace("-", ""))
    if trade_date_dt is None:
        raise MarketTraceSnapshotUnavailable(
            f"Node quick-snapshot trade_date is not a valid calendar date: {trade_date_node!r}"
        )
    if trade_date_normalized != report_date:
        raise MarketTraceSnapshotUnavailable(
            f"Node quick-snapshot trade_date {trade_date_normalized} != report_date {report_date}"
        )

    normalized_a_share = normalize_a_share(close_data)

    # ── 3. 收集外部来源（与 full 版相同逻辑）──
    global_fetch_error: Exception | None = None
    try:
        global_facts = await collect_global_market_facts(captured_at)
    except Exception as e:
        logger.warning("collect_global_market_facts_failed", error_class=type(e).__name__)
        global_facts = []
        global_fetch_error = e

    # 财联社当日全量电报（优先），降级到最新快讯（与 full 版相同逻辑）
    # ── 统一事件抓取中台：证据源优先读事件库，缺库降级到直采（2026-08-12）──
    from aistock_agent.services.event_store import load_event_scrape  # noqa: PLC0415

    # 防御：同 full 版，load_event_scrape 意外抛异常也降级直采（P0 保护）。
    try:
        event_store_events = await load_event_scrape(report_date)
    except Exception as e:  # noqa: BLE001
        logger.warning("event_store_read_failed_fallback_to_direct", error_class=type(e).__name__)
        event_store_events = []
    news_data = None
    news_fetch_error: Exception | None = None
    news_source_kind: str = "telegraph"  # 标记数据来源，供归一化区分字段差异
    if event_store_events:
        logger.info("review_event_store_used", count=len(event_store_events))
        news_source_kind = "event_store"
    else:
        # 缺库降级：走原 telegraph/latest 直采
        try:
            telegraph_data = await node_api.get(
                f"/internal/news/telegraph?date={report_date}&limit=200"
            )
            if telegraph_data is not None:
                news_data = telegraph_data
                news_source_kind = "telegraph"
                _log_telegraph_response(telegraph_data, report_date, snapshot_kind="quick")
        except Exception as e:
            logger.warning("cls_telegraph_fetch_failed", error_class=type(e).__name__)
            news_fetch_error = e

        # 降级：电报接口失败或返回 None 时回退到最新快讯
        if news_data is None:
            try:
                news_data = await node_api.get("/internal/news/latest")
                news_source_kind = "latest"
                # 电报失败但 latest 成功，清除电报阶段的错误标记，
                # 避免 _normalize_news_facts 误判为 unavailable。
                news_fetch_error = None
                logger.info("cls_telegraph_fallback_to_latest", report_date=report_date)
            except Exception as e:
                logger.warning("cls_news_fetch_failed", error_class=type(e).__name__)
                news_fetch_error = e

    tavily_query_1 = f"{report_date} 中国 资本市场 政策 产业 公告"
    tavily_query_2 = f"{report_date} 全球股市 利率 汇率 大宗商品 地缘风险"
    tavily_error_1: Exception | None = None
    tavily_error_2: Exception | None = None
    try:
        tavily_result_1 = TavilyService.search(query=tavily_query_1, topic="news", max_results=5)
    except Exception as e:
        logger.warning("tavily_search_1_failed", error_class=type(e).__name__)
        tavily_result_1 = {}
        tavily_error_1 = e
    try:
        tavily_result_2 = TavilyService.search(query=tavily_query_2, topic="news", max_results=5)
    except Exception as e:
        logger.warning("tavily_search_2_failed", error_class=type(e).__name__)
        tavily_result_2 = {}
        tavily_error_2 = e

    # ── 4. 归一化为 SourceRecord（复用 full 版归一化逻辑）──
    sources: dict[str, SourceRecord] = {}
    missing_fields: list[str] = []
    data_availability = _quick_availability(close_data)

    # ── 3.5. 读取当日晨报预测（失败不阻断，与 full 版保持一致）──
    # quick 版与 full 版同样接入 morning_forecast，便于 15:30 quick snapshot
    # 也带上预判线索；失败/缺失时仅写入 missing_fields，不阻断 snapshot 构建。
    morning_forecast = None
    try:
        morning_forecast = await extract_morning_forecast(report_date)
    except Exception as e:
        logger.warning("morning_forecast_inject_failed", error_class=type(e).__name__)
    if morning_forecast is None:
        _append_missing(missing_fields, "morning_forecast")

    _normalize_index_facts(normalized_a_share, sources, missing_fields, trade_date_dt, captured_at)
    _normalize_aggregate_facts(
        normalized_a_share,
        sources,
        missing_fields,
        trade_date_dt,
        captured_at,
        data_availability,
        is_quick=True,
    )
    if news_source_kind == "event_store":
        cls_news_status = _normalize_event_store_facts(
            event_store_events, sources, missing_fields, captured_at
        )
    else:
        cls_news_status = _normalize_news_facts(
            news_data,
            sources,
            missing_fields,
            captured_at,
            news_fetch_error,
            source_kind=news_source_kind,
        )
    collection_status = {
        "global_markets": _normalize_global_facts(
            global_facts, sources, missing_fields, captured_at, global_fetch_error
        ),
        "cls_news": cls_news_status,
    }
    collection_status.update(
        _normalize_search_facts(
            tavily_result_1,
            tavily_result_2,
            sources,
            missing_fields,
            captured_at,
            (tavily_error_1, tavily_error_2),
        )
    )

    # ── 5. 确定性 discovery ──
    discovery = discover_market_phenomenon(
        normalized_a_share,
        sources,
        captured_at,
        missing_fields,
    )

    # ── 6. 返回事实快照 ──
    trade_date_yyyymmdd = trade_date_node.replace("-", "")
    captured_at_suffix = captured_at.strftime("%Y%m%dT%H%M%S%fZ")
    snapshot_id = f"trace-quick-{trade_date_yyyymmdd}-{captured_at_suffix}"

    return MarketTraceSnapshot(
        snapshot_id=snapshot_id,
        trade_date=report_date,
        captured_at=captured_at,
        a_share=normalized_a_share,
        sources=sources,
        missing_fields=missing_fields,
        phenomenon_discovery=discovery,
        data_availability=data_availability,
        collection_status=collection_status,
        morning_forecast=morning_forecast,
    )
