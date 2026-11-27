"""按 profile 从 Node 真实数据构建 V4 输入。

为什么需要本层：features/ 下的 adapter 只消费冻结 fixture；实时运行必须先
把 Node `/internal/*` 的原始 payload 翻译成同样的输入契约，同时守住三条纪律：
- 只调用 `NodeApiClient` 的只读方法，不在 Python 重复实现 A 股取数；
- 缺字段记录到 `missing_fields` 并降级，绝不编造数值；
- 旧链复盘报告只放进 `legacy_output`，不进入 V4 证据。

时间纪律：`as_of` / `data_cutoff` 全部由调用方显式传入，本模块不读取系统时间。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Literal, Protocol

from aistock_agent.brain_v4.contracts.briefing import Scenario
from aistock_agent.brain_v4.features.briefing.adapter import (
    BriefingChainFixture,
    BriefingEvidenceSeed,
    BriefingHypothesisSeed,
    BriefingSessionSeed,
    EveningScoreSeed,
    EveningSeed,
    MarketTraceLink,
    MiddayChangeSeed,
    MiddaySeed,
    MorningSeed,
)
from aistock_agent.brain_v4.features.market_trace.adapter import (
    HypothesisSeed,
    LegacyComparison,
    MarketTraceCase,
)
from aistock_agent.schemas.market_trace import (
    DataAvailability,
    DataReadiness,
    DetectedPhenomenon,
    MarketTraceSnapshot,
    PhenomenonDiscoveryResult,
    SourceCollectionStatus,
    SourceRecord,
)

#: A 股统一使用东八区；不用系统本地时区，避免服务器时区漂移改变边界。
CST = timezone(timedelta(hours=8))

MORNING_AS_OF = time(8, 30)
MORNING_CUTOFF = time(8, 30)
MIDDAY_AS_OF = time(11, 35)
MIDDAY_CUTOFF = time(11, 30)
EVENING_AS_OF = time(15, 30)
EVENING_CUTOFF = time(15, 0)
MARKET_TRACE_AS_OF = time(15, 35)
MARKET_TRACE_CUTOFF = time(15, 0)

BRIEFING_PROFILES: frozenset[str] = frozenset(
    {"briefing_morning", "briefing_midday", "briefing_evening"}
)


class ReviewReportLike(Protocol):
    """复盘工件读取结果的结构化视图，避免在导入期加载 data_client/settings。"""

    status: str
    report: dict[str, object] | None


class NodeDataSource(Protocol):
    """本模块只依赖 `NodeApiClient` 的这几个只读方法，便于测试注入假客户端。"""

    async def get_close_snapshot(
        self, snapshot_date: str
    ) -> dict[str, object] | None: ...

    async def get_last_close_snapshot(self) -> dict[str, object] | None: ...

    async def get_intraday_sectors(self) -> dict[str, object] | None: ...

    async def get_calendar_events(
        self,
        date_from: str,
        date_to: str,
        *,
        importance: str | None = None,
    ) -> list[dict[str, object]] | None: ...

    async def get_review_analysis_report(self, report_date: date) -> object: ...


@dataclass(frozen=True)
class MarketTraceInputs:
    """市场溯源真实输入的构建结果；缺失项显式暴露，供 runner 写审计。"""

    case: MarketTraceCase
    missing_fields: list[str]
    time_leakage_count: int
    tool_calls: int
    legacy_output: dict[str, object] | None
    #: 行情事实的实际来源：`close_snapshot` / `last_close` / `unavailable`。
    #: 供审计区分「当日收盘快照」与「回退到上一交易日 last_close」。
    data_source: str = "close_snapshot"


@dataclass(frozen=True)
class BriefingChainInputs:
    """早/午/晚真实输入；`evening_extras` 只由收盘行情只读投影。"""

    chain: BriefingChainFixture
    missing_fields: list[str]
    evening_extras: dict[str, object] | None
    time_leakage_count: int
    tool_calls: int
    legacy_outputs: dict[str, object]


class _Fetcher:
    """统一包裹 NodeApiClient 调用：计数并把异常降级为 None。"""

    def __init__(self, client: NodeDataSource) -> None:
        self._client = client
        self.calls = 0
        self.errors = 0

    async def close_snapshot(self, date_compact: str) -> dict[str, object] | None:
        return await self._fetch(self._client.get_close_snapshot(date_compact))

    async def last_close(self) -> dict[str, object] | None:
        return await self._fetch(self._client.get_last_close_snapshot())

    async def intraday_sectors(self) -> dict[str, object] | None:
        return await self._fetch(self._client.get_intraday_sectors())

    async def calendar(self, day_iso: str) -> list[dict[str, object]] | None:
        return await self._fetch_list(
            self._client.get_calendar_events(day_iso, day_iso)
        )

    async def review(self, report_date: date) -> object | None:
        self.calls += 1
        try:
            return await self._client.get_review_analysis_report(report_date)
        except Exception:  # noqa: BLE001 - 取数失败必须降级，不中断 run
            self.errors += 1
            return None

    async def _fetch(self, awaitable: object) -> dict[str, object] | None:
        # 只接受 coroutine：NodeApiClient 的方法是 async，假客户端同契约。
        self.calls += 1
        try:
            value = await _await(awaitable)
        except Exception:  # noqa: BLE001 - 上游异常/超时统一降级
            self.errors += 1
            return None
        return _as_mapping(value)

    async def _fetch_list(
        self, awaitable: object
    ) -> list[dict[str, object]] | None:
        self.calls += 1
        try:
            value = await _await(awaitable)
        except Exception:  # noqa: BLE001 - 上游异常/超时统一降级
            self.errors += 1
            return None
        items = _as_list(value)
        if items is None:
            return None
        records: list[dict[str, object]] = []
        for item in items:
            mapping = _as_mapping(item)
            if mapping is not None:
                records.append(mapping)
        return records


async def _await(value: object) -> object:
    """等待 coroutine 结果；非 coroutine 原样返回，兼容同步假客户端。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def _as_mapping(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, object] = {}
    for key, item in value.items():
        if isinstance(key, str):
            result[key] = item
    return result


def _as_list(value: object) -> list[object] | None:
    if isinstance(value, list):
        return list(value)
    return None


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
    if isinstance(value, float):
        return int(value)
    return None


def _as_datetime(value: object) -> datetime | None:
    """解析 Node 时间字段；naive 时间按东八区解释，统一 aware 便于比较。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=CST)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        seconds = value / 1000 if abs(value) >= 1e11 else float(value)
        try:
            return datetime.fromtimestamp(seconds, tz=UTC).astimezone(CST)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=CST)
    return None


def _at(day: date, clock: time) -> datetime:
    return datetime.combine(day, clock, tzinfo=CST)


def _safe_token(value: str) -> str:
    return "".join(char if char.isalnum() else "_" for char in value).upper()


def _first_index(indexes: Mapping[str, object]) -> tuple[str, dict[str, object]] | None:
    for code in sorted(indexes):
        info = _as_mapping(indexes[code])
        if info is not None:
            return code, info
    return None


def _change_pct(info: Mapping[str, object]) -> float | None:
    for key in ("change_pct", "changePercent", "pct_chg", "change_percent"):
        value = _as_float(info.get(key))
        if value is not None:
            return value
    return None


#: 大小盘基准指数代码（不含交易所后缀，便于匹配 `000300.SH` 等形态）。
_LARGE_CAP_BENCHMARKS: tuple[str, ...] = ("000300", "000016")
_SMALL_CAP_BENCHMARKS: tuple[str, ...] = ("000852", "932000")

#: 基准指数的人类可读名称；仅用于生成派生风格的说明文本，不影响数值。
_INDEX_LABELS: dict[str, str] = {
    "000300": "沪深300",
    "000016": "上证50",
    "000852": "中证1000",
    "932000": "中证2000",
}


def _index_change_pct(info: Mapping[str, object]) -> float | None:
    """从指数条目取涨跌幅；兼容 `change_pct`/`change`/`pct_change` 三种字段名。"""
    for key in ("change_pct", "change", "pct_change"):
        value = _as_float(info.get(key))
        if value is not None:
            return value
    return None


def _normalize_indexes(raw: object) -> dict[str, dict[str, object]] | None:
    """把快照 `indexes` 归一化成 `{code: info}` 映射。

    为什么需要：服务器探针确认 close/last_close 的 `indexes` 实际是 list
    （6 个元素），旧实现用 `_as_mapping` 直接把 list 判成 None，导致 indexes
    长期误报缺失。这里同时兼容 list 与 dict 两种形态：list 元素用稳定键
    （优先 `code`，其次 `symbol`，再退化为 `name`）归一化；非 mapping 元素
    跳过；全空返回 None，交由调用方走 missing 分支。
    """
    if isinstance(raw, Mapping):
        result: dict[str, dict[str, object]] = {}
        for key, value in raw.items():
            if not isinstance(key, str):
                continue
            info = _as_mapping(value)
            if info is None:
                continue
            result[key] = info
        return result or None
    if isinstance(raw, Sequence) and not isinstance(raw, str | bytes):
        result = {}
        for item in raw:
            info = _as_mapping(item)
            if info is None:
                continue
            code = (
                _as_str(info.get("code"))
                or _as_str(info.get("symbol"))
                or _as_str(info.get("name"))
            )
            if code is None:
                continue
            result[code] = info
        return result or None
    return None


def _match_benchmark(
    indexes: Mapping[str, Mapping[str, object]],
    candidates: tuple[str, ...],
) -> tuple[str, float] | None:
    """按候选代码前缀匹配基准指数并取涨跌幅；缺涨跌幅视为不可用。"""
    for target in candidates:
        for code in sorted(indexes):
            bare = code.split(".")[0]
            if bare == target or code.startswith(target):
                change = _index_change_pct(indexes[code])
                if change is not None:
                    return code, change
    return None


def _derive_style(
    indexes: Mapping[str, Mapping[str, object]] | None,
) -> dict[str, object] | None:
    """用真实基准指数涨跌幅确定性推导大小盘风格；缺任一侧基准返回 None。

    规则（method=index_spread_v1）：`spread = 大盘基准涨跌幅 - 小盘基准涨跌幅`，
    |spread| < 0.2 视为均衡。绝不使用单一指数猜测。
    """
    if not indexes:
        return None
    large = _match_benchmark(indexes, _LARGE_CAP_BENCHMARKS)
    small = _match_benchmark(indexes, _SMALL_CAP_BENCHMARKS)
    if large is None or small is None:
        return None
    large_code, large_change = large
    small_code, small_change = small
    spread = round(large_change - small_change, 4)
    if abs(spread) < 0.2:
        label = "均衡"
    elif spread > 0:
        label = "大盘占优"
    else:
        label = "小盘占优"
    return {
        "large_cap_code": large_code,
        "large_cap_change": large_change,
        "small_cap_code": small_code,
        "small_cap_change": small_change,
        "spread": spread,
        "label": label,
        "source": "derived:indexes",
        "method": "index_spread_v1",
    }


def _format_style(style: Mapping[str, object]) -> str:
    """把派生风格渲染成一行人类可读说明。"""
    large_code = _as_str(style.get("large_cap_code")) or ""
    small_code = _as_str(style.get("small_cap_code")) or ""
    large_change = _as_float(style.get("large_cap_change")) or 0.0
    small_change = _as_float(style.get("small_cap_change")) or 0.0
    spread = _as_float(style.get("spread")) or 0.0
    label = _as_str(style.get("label")) or "均衡"
    large_name = _INDEX_LABELS.get(large_code.split(".")[0], large_code)
    small_name = _INDEX_LABELS.get(small_code.split(".")[0], small_code)
    return (
        f"{large_name} {large_change:+.2f}% vs {small_name} {small_change:+.2f}%，"
        f"价差 {spread:.2f} → {label}"
    )


def _severity(change: float | None) -> Literal["low", "medium", "high"]:
    magnitude = abs(change) if change is not None else 0.0
    if magnitude >= 1.5:
        return "high"
    if magnitude >= 0.8:
        return "medium"
    return "low"


def _direction(change: float | None) -> Literal["up", "down", "range"]:
    if change is None:
        return "range"
    if change <= -0.5:
        return "down"
    if change >= 0.5:
        return "up"
    return "range"


def _source_record(
    *,
    source_id: str,
    kind: str,
    provider: str,
    title: str,
    content: str,
    occurred_at: datetime | None,
    captured_at: datetime,
    source_level: str,
) -> SourceRecord:
    # 单一职责来源记录：时间和文本全部来自上游 payload，不补造数值。
    return SourceRecord(
        source_id=source_id,
        kind="event_evidence" if kind == "event_evidence" else "market_fact",
        provider=provider,
        title=title,
        content=content,
        url=None,
        occurred_at=occurred_at,
        captured_at=captured_at,
        source_level=(
            "market_data"
            if source_level == "market_data"
            else "primary"
            if source_level == "primary"
            else "reporting"
        ),
    )


def _format_pairs(info: Mapping[str, object]) -> str:
    parts = [
        f"{key}={info[key]}"
        for key in sorted(info)
        if isinstance(info[key], str | int | float | bool)
    ]
    return ", ".join(parts) or "no_scalar_fields"


def _family(source_id: str) -> str:
    return source_id.split("_", 1)[0].lower()


def _availability(state: str, fields: Sequence[str], reason: str | None) -> DataAvailability:
    # 显式把 str 收敛到 Literal；未知值按 partial 处理，绝不默认 available。
    if state == "available":
        return DataAvailability(
            state="available", available_fields=list(fields), reason=reason
        )
    if state == "partial":
        return DataAvailability(
            state="partial", available_fields=list(fields), reason=reason
        )
    return DataAvailability(
        state="unavailable", available_fields=list(fields), reason=reason
    )


def _collection(state: str, provider: str, count: int, reason: str | None) -> SourceCollectionStatus:
    if state in {"available", "empty", "unavailable", "invalid_for_causality", "degraded"}:
        return SourceCollectionStatus(
            state=state,  # type: ignore[arg-type]
            provider=provider,
            item_count=count,
            reason=reason,
        )
    return SourceCollectionStatus(
        state="unavailable", provider=provider, item_count=count, reason=reason
    )


def _review_fields(review: object | None) -> tuple[str, dict[str, object] | None]:
    if review is None:
        return "unavailable", None
    status = getattr(review, "status", "unavailable")
    report = getattr(review, "report", None)
    return (status if isinstance(status, str) else "unavailable"), _as_mapping(report)


async def build_market_trace_case(
    client: NodeDataSource,
    as_of: datetime,
    data_cutoff: datetime,
    snapshot_id: str,
) -> MarketTraceInputs:
    """从真实数据构建一个市场溯源案例；缺失字段记录后降级，不编造数值。"""
    fetcher = _Fetcher(client)
    day = as_of.date()
    date_compact = day.strftime("%Y%m%d")
    close = await fetcher.close_snapshot(date_compact)
    # 当日收盘快照可能尚未生成（close-snapshot 409/None）。此时回退到
    # last_close_snapshot 填充 indexes/breadth/sectors，而不是把这些字段判缺；
    # 同时显式标注 data_source，避免把上一交易日数据伪装成当日。
    data_source = "close_snapshot"
    if close is None:
        fallback = await fetcher.last_close()
        if fallback is not None:
            close = fallback
            data_source = "last_close"
        else:
            data_source = "unavailable"
    provider = (
        "node_api:last-close-snapshot"
        if data_source == "last_close"
        else "node_api:close-snapshot"
    )
    calendar = await fetcher.calendar(day.isoformat())
    review_status, legacy_report = _review_fields(
        await fetcher.review(day)
    )

    missing: list[str] = []
    if close is None:
        missing.append("close_snapshot")
    if calendar is None:
        missing.append("calendar_events")
    # legacy_review 只做对照，不参与 V4 完整性判定，因此不写入 missing_fields。

    captured_at = as_of
    if close is not None:
        captured_at = _as_datetime(close.get("captured_at")) or as_of

    a_share: dict[str, object] = {}
    sources: dict[str, SourceRecord] = {}
    availability: dict[str, DataAvailability] = {}
    collection: dict[str, SourceCollectionStatus] = {}
    index_code: str | None = None
    index_info: dict[str, object] | None = None
    change: float | None = None

    # 快照 `indexes` 是 list 形态（探针确认），必须归一化成 {code: info}；
    # 否则 list 被 _as_mapping 判成 None 会让 indexes 长期误报缺失。
    indexes = _normalize_indexes(close.get("indexes")) if close is not None else None
    if indexes:
        a_share["indexes"] = indexes
        for code in sorted(indexes):
            info = _as_mapping(indexes[code])
            if info is None:
                continue
            if index_code is None:
                index_code, index_info = code, info
                change = _change_pct(info)
            source_id = f"INDEX_{_safe_token(code)}"
            sources[source_id] = _source_record(
                source_id=source_id,
                kind="market_fact",
                provider=_as_str(info.get("source")) or provider,
                title=_as_str(info.get("name")) or code,
                content=_format_pairs(info),
                occurred_at=data_cutoff,
                captured_at=captured_at,
                source_level="market_data",
            )
        availability["index"] = _availability(
            "available", ["close", "change_pct"], None
        )
        collection["index"] = _collection(
            "available", provider, len(indexes), None
        )
    else:
        missing.append("indexes")
        availability["index"] = _availability("unavailable", [], "missing indexes")
        collection["index"] = _collection(
            "unavailable", provider, 0, "missing indexes"
        )

    breadth = _as_mapping(close.get("breadth")) if close is not None else None
    if breadth:
        a_share["breadth"] = breadth
        sources["BREADTH_ALL"] = _source_record(
            source_id="BREADTH_ALL",
            kind="market_fact",
            provider=provider,
            title="全市场涨跌家数",
            content=_format_pairs(breadth),
            occurred_at=data_cutoff,
            captured_at=captured_at,
            source_level="market_data",
        )
        availability["breadth"] = _availability(
            "available", ["advance_count", "decline_count"], None
        )
        collection["breadth"] = _collection(
            "available", provider, 1, None
        )
    else:
        missing.append("breadth")
        availability["breadth"] = _availability("unavailable", [], "missing breadth")
        collection["breadth"] = _collection(
            "unavailable", provider, 0, "missing breadth"
        )

    # style 键在真实快照中不存在：当上游未提供 style 时，用 indexes 的真实
    # 涨跌幅确定性推导大小盘风格；provider 标 derived:indexes，绝不编造数值。
    derived_style = (
        _derive_style(indexes)
        if close is not None and _as_mapping(close.get("style")) is None
        else None
    )

    for key, source_id, title in (
        ("turnover", "TURNOVER_ALL", "全市场成交额"),
        ("style", "STYLE_ALL", "大小盘风格"),
        ("sectors", "SECTOR_ALL", "板块涨跌"),
    ):
        section = _as_mapping(close.get(key)) if close is not None else None
        family = _family(source_id)
        if section:
            a_share[key] = section
            sources[source_id] = _source_record(
                source_id=source_id,
                kind="market_fact",
                provider=provider,
                title=title,
                content=_format_pairs(section)
                if key != "sectors"
                else "; ".join(
                    f"{name}:{_format_pairs(mapping)}"
                    for name in sorted(section)
                    for mapping in [_as_mapping(section[name]) or {}]
                ),
                occurred_at=data_cutoff,
                captured_at=captured_at,
                source_level="market_data",
            )
            availability[family] = _availability("available", [key], None)
            collection[family] = _collection(
                "available", provider, 1, None
            )
        elif key == "style" and derived_style is not None:
            # 派生数据：来源标 derived:indexes，source_level 走 schema 允许的
            # reporting（SourceRecord 的 Literal 无 derived 取值）。
            a_share[key] = derived_style
            sources[source_id] = _source_record(
                source_id=source_id,
                kind="market_fact",
                provider="derived:indexes",
                title=title,
                content=_format_style(derived_style),
                occurred_at=data_cutoff,
                captured_at=captured_at,
                source_level="reporting",
            )
            availability[family] = _availability(
                "available", ["indexes"], "derived from indexes"
            )
            collection[family] = _collection(
                "available", "derived:indexes", 1, None
            )
        else:
            missing.append(key)
            availability[family] = _availability("unavailable", [], f"missing {key}")
            collection[family] = _collection(
                "unavailable", provider, 0, f"missing {key}"
            )

    visible_events: list[dict[str, object]] = []
    leakage = 0
    for event in calendar or []:
        event_time = _event_time(event)
        published_at = _event_published(event) or event_time
        if event_time is None:
            continue
        if event_time > data_cutoff or (
            published_at is not None and published_at > as_of
        ):
            leakage += 1
            continue
        visible_events.append(event)

    for position, event in enumerate(
        sorted(visible_events, key=lambda item: (_event_sort_key(item), _event_title(item)))
    ):
        source_id = f"CAL_{position:02d}"
        event_time = _event_time(event)
        published_at = _event_published(event) or event_time or data_cutoff
        sources[source_id] = _source_record(
            source_id=source_id,
            kind="event_evidence",
            provider=_as_str(event.get("source")) or "node_api:calendar",
            title=_event_title(event),
            content=_event_content(event),
            occurred_at=event_time,
            captured_at=published_at,
            source_level="reporting",
        )
    if calendar is not None:
        availability["cal"] = _availability(
            "available" if visible_events else "partial",
            ["title"],
            None if visible_events else "no visible calendar events",
        )
        collection["cal"] = _collection(
            "available" if visible_events else "empty",
            "node_api:calendar",
            len(visible_events),
            None,
        )
    else:
        availability["cal"] = _availability("unavailable", [], "missing calendar")
        collection["cal"] = _collection(
            "unavailable", "node_api:calendar", 0, "missing calendar"
        )

    discovery = _phenomenon_discovery(
        index_code=index_code,
        index_info=index_info,
        change=change,
        breadth=breadth,
        close_available=close is not None,
    )

    snapshot = MarketTraceSnapshot(
        snapshot_id=snapshot_id,
        trade_date=(
            _as_str(close.get("trade_date")) or date_compact
            if close is not None
            else date_compact
        ),
        captured_at=captured_at,
        a_share=a_share,
        sources=sources,
        missing_fields=sorted(set(missing)),
        phenomenon_discovery=discovery,
        data_availability=availability,
        collection_status=collection,
    )

    subject_code = index_code or "000001.SH"
    case = MarketTraceCase(
        case_id=f"case_market_trace_{date_compact}",
        case_kind="live_real_data",
        run_id=f"v4_{day.isoformat()}_market_trace_1",
        subject_code=subject_code,
        materiality=_materiality(change),
        as_of=as_of,
        data_cutoff=data_cutoff,
        snapshot=snapshot,
        hypothesis_seeds=_market_trace_seeds(),
        legacy=LegacyComparison(
            run_id=f"legacy_review_{date_compact}",
            output={
                "status": review_status,
                "report": legacy_report,
            },
        ),
    )
    return MarketTraceInputs(
        case=case,
        missing_fields=sorted(set(missing)),
        time_leakage_count=leakage,
        tool_calls=fetcher.calls,
        legacy_output=legacy_report,
        data_source=data_source,
    )


def _market_trace_seeds() -> list[HypothesisSeed]:
    """固定研究起点：假设文本是待检验命题，不携带任何行情数值。"""
    return [
        HypothesisSeed(
            hypothesis_id="hyp_liquidity",
            statement="流动性收缩是结构根因",
            prior_confidence=0.60,
            assumptions=["成交额较前一交易日下降"],
            disconfirmers=["成交额转为放大"],
            required_evidence_types=["market_fact"],
            scope="market",
        ),
        HypothesisSeed(
            hypothesis_id="hyp_style_divergence",
            statement="风格分化是主要驱动",
            prior_confidence=0.45,
            assumptions=["成长板块弱于防御板块"],
            disconfirmers=["大小盘同步下跌"],
            required_evidence_types=["market_fact"],
            scope="market",
        ),
        HypothesisSeed(
            hypothesis_id="hyp_sector_rotation",
            statement="板块轮动是次级因素",
            prior_confidence=0.35,
            assumptions=["板块涨跌出现分化"],
            disconfirmers=["全板块同涨同跌"],
            required_evidence_types=["market_fact"],
            scope="market",
        ),
    ]


def _materiality(change: float | None) -> Literal["low", "medium", "high"]:
    magnitude = abs(change) if change is not None else 0.0
    if magnitude >= 1.5:
        return "high"
    if magnitude >= 0.8:
        return "medium"
    return "low"


def _phenomenon_discovery(
    *,
    index_code: str | None,
    index_info: dict[str, object] | None,
    change: float | None,
    breadth: dict[str, object] | None,
    close_available: bool,
) -> PhenomenonDiscoveryResult:
    """只用已有指数/宽度事实判定现象；缺数据时明确 insufficient_data。"""
    readiness = DataReadiness(
        market_data="complete" if close_available else "incomplete",
        attribution_inputs=(
            "complete"
            if index_info is not None and breadth is not None
            else "partial"
            if index_info is not None or breadth is not None
            else "missing"
        ),
        causal_evidence=(
            "ready"
            if index_info is not None and breadth is not None
            else "partial"
            if index_info is not None
            else "not_ready"
        ),
    )
    if index_info is None or breadth is None:
        return PhenomenonDiscoveryResult(
            status="insufficient_data",
            primary=None,
            concurrent_phenomena=[],
            data_readiness=readiness,
            diagnostics=[],
        )

    kind: Literal[
        "broad_decline", "broad_rally", "style_divergence"
    ] = (
        "broad_decline"
        if change is not None and change <= -0.5
        else "broad_rally"
        if change is not None and change >= 0.5
        else "style_divergence"
    )
    name = _as_str(index_info.get("name")) or "指数"
    summary = (
        f"{name} 收盘涨跌 {change:+.2f}%" if change is not None else f"{name} 收盘"
    )
    primary = DetectedPhenomenon(
        kind=kind,
        summary=summary,
        fact_ids=[
            f"INDEX_{_safe_token(index_code or _as_str(index_info.get('ts_code')) or '000001.SH')}",
            "BREADTH_ALL",
        ],
        tags=["index", "breadth"],
        severity=_severity(change),
    )
    return PhenomenonDiscoveryResult(
        status="detected",
        primary=primary,
        concurrent_phenomena=[],
        data_readiness=readiness,
        diagnostics=[],
    )


def _event_time(event: Mapping[str, object]) -> datetime | None:
    explicit = _as_datetime(event.get("event_time"))
    if explicit is not None:
        return explicit
    day_value = _as_datetime(event.get("event_date"))
    if day_value is None:
        return None
    # 只有日期没有时刻时按当日 00:00 处理：这是“日粒度事实”，不补造盘中时点。
    return _at(day_value.date(), time(0, 0))


def _event_published(event: Mapping[str, object]) -> datetime | None:
    for key in ("published_at", "publish_time", "created_at"):
        parsed = _as_datetime(event.get(key))
        if parsed is not None:
            return parsed
    return None


def _event_title(event: Mapping[str, object]) -> str:
    return _as_str(event.get("title")) or "calendar event"


def _event_content(event: Mapping[str, object]) -> str:
    return (
        _as_str(event.get("content"))
        or _as_str(event.get("detail"))
        or _event_title(event)
    )


def _event_sort_key(event: Mapping[str, object]) -> str:
    value = _event_time(event)
    return value.isoformat() if value is not None else ""


def _event_tags(event: Mapping[str, object]) -> list[str]:
    text = f"{_event_title(event)} {_event_content(event)}"
    tags: list[str] = []
    if any(token in text for token in ("风格", "大小盘", "成长", "防御")):
        tags.append("style")
    if any(token in text for token in ("成交额", "流动性", "资金")):
        tags.append("liquidity")
    if any(token in text for token in ("板块", "行业", "轮动")):
        tags.append("sector")
    importance = _as_str(event.get("importance"))
    if importance is not None:
        tags.append(f"importance:{importance.lower()}")
    return tags


async def build_briefing_chain(
    client: NodeDataSource,
    day: date,
    profile: str,
) -> BriefingChainInputs:
    """构建一个交易日的早/午/晚真实输入链；profile 仅用于校验目标时段。"""
    if profile not in BRIEFING_PROFILES:
        raise ValueError(f"unknown briefing profile: {profile!r}")

    fetcher = _Fetcher(client)
    date_iso = day.isoformat()
    date_compact = day.strftime("%Y%m%d")
    morning_as_of = _at(day, MORNING_AS_OF)
    morning_cutoff = _at(day, MORNING_CUTOFF)
    midday_as_of = _at(day, MIDDAY_AS_OF)
    midday_cutoff = _at(day, MIDDAY_CUTOFF)
    evening_as_of = _at(day, EVENING_AS_OF)
    evening_cutoff = _at(day, EVENING_CUTOFF)

    close = await fetcher.close_snapshot(date_compact)
    last_close = await fetcher.last_close()
    intraday = await fetcher.intraday_sectors()
    calendar = await fetcher.calendar(date_iso)
    review_status, legacy_report = _review_fields(await fetcher.review(day))

    missing: list[str] = []
    if close is None:
        missing.append("close_snapshot")
    if last_close is None:
        missing.append("last_close_snapshot")
    if intraday is None:
        missing.append("intraday_sectors")
    if calendar is None:
        missing.append("calendar_events")
    # 旧链复盘只进 legacy_output；缺失不得把 V4 状态降级。
    items: list[BriefingEvidenceSeed] = []
    items.extend(_previous_close_items(last_close, morning_as_of))
    items.extend(_calendar_items(calendar or []))
    items.extend(_close_items(close, evening_as_of))
    intraday_items = _intraday_items(intraday, midday_cutoff, midday_as_of)
    items.extend(intraday_items)
    # 固定按 source_id 排序，保证同一输入每次得到同一证据顺序。
    items.sort(key=lambda seed: seed.source_id)

    morning_visible = [seed for seed in items if _visible(seed, morning_cutoff, morning_as_of)]
    midday_visible = [seed for seed in items if _visible(seed, midday_cutoff, midday_as_of)]

    sessions = [
        BriefingSessionSeed(
            profile="briefing_morning",
            run_id=f"v4_{date_iso}_briefing_morning_1",
            snapshot_id=f"snap_briefing_morning_{date_compact}",
            as_of=morning_as_of,
            data_cutoff=morning_cutoff,
            legacy_run_id=f"legacy_briefing_morning_{date_compact}",
            legacy_headline=_legacy_headline(legacy_report, review_status),
            legacy_evidence_ids=_legacy_evidence_ids(legacy_report),
        ),
        BriefingSessionSeed(
            profile="briefing_midday",
            run_id=f"v4_{date_iso}_briefing_midday_1",
            snapshot_id=f"snap_briefing_midday_{date_compact}",
            as_of=midday_as_of,
            data_cutoff=midday_cutoff,
            legacy_run_id=f"legacy_briefing_midday_{date_compact}",
            legacy_headline=_legacy_headline(legacy_report, review_status),
            legacy_evidence_ids=_legacy_evidence_ids(legacy_report),
        ),
        BriefingSessionSeed(
            profile="briefing_evening",
            run_id=f"v4_{date_iso}_briefing_evening_1",
            snapshot_id=f"snap_briefing_evening_{date_compact}",
            as_of=evening_as_of,
            data_cutoff=evening_cutoff,
            legacy_run_id=f"legacy_briefing_evening_{date_compact}",
            legacy_headline=_legacy_headline(legacy_report, review_status),
            legacy_evidence_ids=_legacy_evidence_ids(legacy_report),
        ),
    ]

    hypotheses = _briefing_hypotheses()
    changes = _midday_changes(hypotheses, midday_visible, morning_visible)
    scores = _evening_scores(hypotheses, changes)
    close_index = _close_index(close)
    close_change = _change_pct(close_index) if close_index is not None else None
    breadth = _as_mapping(close.get("breadth")) if close is not None else None

    morning = _morning_seed(
        last_close=last_close,
        visible=morning_visible,
    )
    midday = _midday_seed(
        intraday=intraday,
        new_drivers=[
            seed.title
            for seed in midday_visible
            if seed.source_id.startswith("CAL_")
            and all(seed.source_id != old.source_id for old in morning_visible)
        ],
    )
    evening = _evening_seed(
        close_index=close_index,
        change=close_change,
        breadth=breadth,
        missing=missing,
    )

    chain = BriefingChainFixture(
        chain_id=f"chain_{date_compact}_briefing_live",
        trading_date=day,
        subject_code="000001.SH",
        sessions=sessions,
        items=items,
        hypotheses=hypotheses,
        midday_changes=changes,
        evening_scores=scores,
        morning=morning,
        midday=midday,
        evening=evening,
        market_trace=MarketTraceLink(
            case_id=f"live_market_trace_{date_compact}",
            case_fixture=f"live:{date_iso}",
            judgment_role="evening_closing_attribution",
        ),
    )
    return BriefingChainInputs(
        chain=chain,
        missing_fields=sorted(set(missing)),
        evening_extras=_evening_extras(close),
        time_leakage_count=0,
        tool_calls=fetcher.calls,
        legacy_outputs={
            "status": review_status,
            "report": legacy_report,
        },
    )


def _visible(seed: BriefingEvidenceSeed, cutoff: datetime, as_of: datetime) -> bool:
    return seed.event_time <= cutoff and seed.published_at <= as_of


def _seed(
    *,
    source_id: str,
    source: str,
    source_type: str,
    title: str,
    content: str,
    event_time: datetime,
    published_at: datetime,
    tags: Sequence[str] = (),
    time_ok: bool = True,
) -> BriefingEvidenceSeed:
    # published_at 缺失时上游按 event_time 回填，这里显式降级质量，绝不默认 high。
    quality: Literal["high", "low"] = "high" if time_ok else "low"
    return BriefingEvidenceSeed(
        source_id=source_id,
        source=source,
        source_type=source_type,
        title=title,
        content=content,
        event_time=event_time,
        published_at=published_at,
        data_quality=quality,
        time_confidence=quality,
        tags=list(tags),
    )


def _previous_close_items(
    last_close: dict[str, object] | None, morning_as_of: datetime
) -> list[BriefingEvidenceSeed]:
    if last_close is None:
        return []
    captured_at = _as_datetime(last_close.get("captured_at")) or morning_as_of
    trade_date = _as_datetime(last_close.get("trade_date"))
    event_time = (
        _at(trade_date.date(), time(15, 0))
        if trade_date is not None
        else _at(morning_as_of.date() - timedelta(days=1), time(15, 0))
    )
    items: list[BriefingEvidenceSeed] = []
    indexes = _normalize_indexes(last_close.get("indexes"))
    if indexes:
        for code in sorted(indexes):
            info = _as_mapping(indexes[code])
            if info is None:
                continue
            items.append(
                _seed(
                    source_id=f"PREV_INDEX_{_safe_token(code)}",
                    source="node_api:last-close-snapshot",
                    source_type="market_fact",
                    title=_as_str(info.get("name")) or code,
                    content=_format_pairs(info),
                    event_time=event_time,
                    published_at=captured_at,
                    tags=["index"],
                )
            )
    breadth = _as_mapping(last_close.get("breadth"))
    if breadth:
        items.append(
            _seed(
                source_id="PREV_BREADTH_ALL",
                source="node_api:last-close-snapshot",
                source_type="market_fact",
                title="上一交易日涨跌家数",
                content=_format_pairs(breadth),
                event_time=event_time,
                published_at=captured_at,
                tags=["breadth"],
            )
        )
    return items


def _calendar_items(
    events: Sequence[dict[str, object]],
) -> list[BriefingEvidenceSeed]:
    items: list[BriefingEvidenceSeed] = []
    ordered = sorted(events, key=lambda item: (_event_sort_key(item), _event_title(item)))
    for position, event in enumerate(ordered):
        event_time = _event_time(event)
        if event_time is None:
            continue
        published = _event_published(event)
        time_ok = published is not None
        items.append(
            _seed(
                source_id=f"CAL_{position:02d}",
                source=_as_str(event.get("source")) or "node_api:calendar",
                source_type="event_evidence",
                title=_event_title(event),
                content=_event_content(event),
                event_time=event_time,
                published_at=published or event_time,
                tags=_event_tags(event),
                time_ok=time_ok,
            )
        )
    return items


def _close_items(
    close: dict[str, object] | None, evening_as_of: datetime
) -> list[BriefingEvidenceSeed]:
    if close is None:
        return []
    captured_at = _as_datetime(close.get("captured_at")) or evening_as_of
    event_time = _at(evening_as_of.date(), time(15, 0))
    items: list[BriefingEvidenceSeed] = []
    indexes = _normalize_indexes(close.get("indexes"))
    if indexes:
        for code in sorted(indexes):
            info = _as_mapping(indexes[code])
            if info is None:
                continue
            items.append(
                _seed(
                    source_id=f"INDEX_{_safe_token(code)}",
                    source="node_api:close-snapshot",
                    source_type="market_fact",
                    title=_as_str(info.get("name")) or code,
                    content=_format_pairs(info),
                    event_time=event_time,
                    published_at=captured_at,
                    tags=["index"],
                )
            )
    # 收盘路径同样兼容 list 形态 indexes，并在缺失 style 时确定性派生。
    derived_style = (
        _derive_style(indexes) if _as_mapping(close.get("style")) is None else None
    )
    for key, source_id, title, tags in (
        ("breadth", "BREADTH_ALL", "收盘涨跌家数", ["breadth"]),
        ("turnover", "TURNOVER_ALL", "收盘成交额", ["liquidity"]),
        ("style", "STYLE_ALL", "收盘风格", ["style"]),
        ("sectors", "SECTOR_ALL", "收盘板块", ["sector"]),
    ):
        section = _as_mapping(close.get(key))
        if not section:
            if key == "style" and derived_style is not None:
                items.append(
                    _seed(
                        source_id=source_id,
                        source="derived:indexes",
                        source_type="market_fact",
                        title=title,
                        content=_format_style(derived_style),
                        event_time=event_time,
                        published_at=captured_at,
                        tags=tags,
                    )
                )
            continue
        items.append(
            _seed(
                source_id=source_id,
                source="node_api:close-snapshot",
                source_type="market_fact",
                title=title,
                content=_format_pairs(section),
                event_time=event_time,
                published_at=captured_at,
                tags=tags,
            )
        )
    return items


def _intraday_items(
    intraday: dict[str, object] | None,
    cutoff: datetime,
    as_of: datetime,
) -> list[BriefingEvidenceSeed]:
    if intraday is None:
        return []
    captured_at = _as_datetime(intraday.get("captured_at")) or as_of
    gainers = _as_list(intraday.get("gainers")) or []
    losers = _as_list(intraday.get("losers")) or []
    content_parts: list[str] = []
    for label, rows in (("gainers", gainers), ("losers", losers)):
        for row in rows:
            info = _as_mapping(row)
            if info is not None:
                content_parts.append(f"{label}:{_format_pairs(info)}")
    return [
        _seed(
            source_id="SECTOR_INTRADAY",
            source="node_api:intraday-sectors",
            source_type="market_fact",
            title="上午板块快照",
            content="; ".join(content_parts) or "no sector rows",
            event_time=cutoff,
            published_at=captured_at,
            tags=["sector", "style"],
        )
    ]


def _briefing_hypotheses() -> list[BriefingHypothesisSeed]:
    return [
        BriefingHypothesisSeed(
            claim_ref="hyp_morning_style",
            statement="风格分化导致成长领跌",
            material=True,
            confidence=0.45,
        ),
        BriefingHypothesisSeed(
            claim_ref="hyp_morning_liquidity",
            statement="流动性收缩是今日下跌主因",
            material=True,
            confidence=0.55,
        ),
        BriefingHypothesisSeed(
            claim_ref="hyp_morning_sector",
            statement="板块轮动是次级因素",
            material=True,
            confidence=0.35,
        ),
    ]


def _midday_changes(
    hypotheses: Sequence[BriefingHypothesisSeed],
    midday_visible: Sequence[BriefingEvidenceSeed],
    morning_visible: Sequence[BriefingEvidenceSeed],
) -> list[MiddayChangeSeed]:
    morning_ids = {seed.source_id for seed in morning_visible}
    changes: list[MiddayChangeSeed] = []
    kind_by_ref = {
        "hyp_morning_style": "style",
        "hyp_morning_liquidity": "liquidity",
        "hyp_morning_sector": "sector",
    }
    for hypothesis in hypotheses:
        kind = kind_by_ref.get(hypothesis.claim_ref)
        if kind is None:
            continue
        candidate = next(
            (
                seed
                for seed in midday_visible
                if seed.source_id not in morning_ids and kind in seed.tags
            ),
            None,
        )
        if candidate is None:
            continue
        text = f"{candidate.title} {candidate.content}"
        relation = (
            "reverse"
            if kind == "liquidity"
            and any(token in text for token in ("放大", "反向", "否定", "相反"))
            else "confirm"
        )
        changes.append(
            MiddayChangeSeed(
                claim_ref=hypothesis.claim_ref,
                relation=relation,  # type: ignore[arg-type]
                reason=f"{candidate.title}：{candidate.content}",
                evidence_source_ids=[candidate.source_id],
            )
        )
    return sorted(changes, key=lambda change: change.claim_ref)


def _evening_scores(
    hypotheses: Sequence[BriefingHypothesisSeed],
    changes: Sequence[MiddayChangeSeed],
) -> list[EveningScoreSeed]:
    relation_by_ref = {change.claim_ref: change.relation for change in changes}
    scores: list[EveningScoreSeed] = []
    for hypothesis in hypotheses:
        relation = relation_by_ref.get(hypothesis.claim_ref)
        if relation == "confirm":
            verdict, score = "confirmed", 0.70
        elif relation == "reverse":
            verdict, score = "reversed", 0.30
        else:
            verdict, score = "unresolved", 0.50
        scores.append(
            EveningScoreSeed(
                claim_ref=hypothesis.claim_ref,
                source_profile="briefing_morning",
                verdict=verdict,  # type: ignore[arg-type]
                score=score,
            )
        )
    return sorted(scores, key=lambda item: item.claim_ref)


def _close_index(close: dict[str, object] | None) -> dict[str, object] | None:
    if close is None:
        return None
    indexes = _as_mapping(close.get("indexes"))
    if not indexes:
        return None
    first = _first_index(indexes)
    return first[1] if first is not None else None


def _morning_seed(
    *,
    last_close: dict[str, object] | None,
    visible: Sequence[BriefingEvidenceSeed],
) -> MorningSeed:
    index = _close_index(last_close)
    change = _change_pct(index) if index is not None else None
    name = _as_str(index.get("name")) if index is not None else None
    close_value = _as_float(index.get("close")) if index is not None else None
    if name is not None and change is not None:
        headline = f"早报：{name}上一交易日收于 {close_value if close_value is not None else 'NA'}（{change:+.2f}%）"
        summary = (
            f"上一交易日{name}收报 {close_value if close_value is not None else 'NA'}，"
            "盘前关注成交额与风格分化。"
        )
    else:
        headline = "早报：盘前快照不可用，降级输出"
        summary = "盘前基线数据缺失，早报只保留自身时间边界内的日历事件。"

    overnight = [seed.title for seed in visible if seed.source_id.startswith("CAL_")][:3]
    breadth = _as_mapping(last_close.get("breadth")) if last_close is not None else None
    if breadth is not None:
        today_baseline = (
            f"上一交易日上涨 {_as_int(breadth.get('advance_count')) or 0} 家、"
            f"下跌 {_as_int(breadth.get('decline_count')) or 0} 家。"
        )
    else:
        today_baseline = "上一交易日基线数据不足，关注开盘后成交额与涨跌家数。"

    risk_flags = [
        seed.title
        for seed in visible
        if any(tag == "importance:high" for tag in seed.tags)
    ][:3]
    return MorningSeed(
        headline=headline,
        summary=summary,
        direction=_direction(change),
        confidence=0.50,
        risk_level=_severity(change),
        overnight_changes=overnight,
        today_baseline=today_baseline,
        key_event_source_ids=[seed.source_id for seed in visible][:3],
        style_sector_hypotheses=["风格分化：关注大小盘相对强弱", "板块轮动：关注防御与成长相对表现"],
        scenarios=[
            Scenario(
                scenario_id="scn_base",
                label="基准情景",
                trigger_conditions=["开盘后成交额与上一交易日相当"],
                expected_path="指数维持区间震荡，风格再平衡",
                invalidator="若成交额显著放大且指数单边运行，则本情景失效",
            ),
            Scenario(
                scenario_id="scn_risk",
                label="风险情景",
                trigger_conditions=["成长板块继续弱于防御"],
                expected_path="指数承压下行，成长板块跌幅扩大",
                invalidator="若成长板块逆势领涨，则本情景失效",
            ),
        ],
        invalidators=[
            "若成交额显著放大，则流动性收缩假设失效",
            "若大小盘同步下跌，则风格分化假设失效",
        ],
        risk_flags=risk_flags,
    )


def _midday_seed(
    *,
    intraday: dict[str, object] | None,
    new_drivers: Sequence[str],
) -> MiddaySeed:
    indexes = _as_mapping(intraday.get("indexes")) if intraday is not None else None
    first = _first_index(indexes) if indexes else None
    change = _change_pct(first[1]) if first is not None else None
    name = _as_str(first[1].get("name")) if first is not None else None
    gainers = _sector_names(intraday, "gainers")
    losers = _sector_names(intraday, "losers")
    if name is not None and change is not None:
        headline = f"午报：上午{name} {change:+.2f}%"
        summary = (
            f"上午{name}涨跌 {change:+.2f}%；"
            f"{('、'.join(gainers[:2]) or '暂无明确领涨')}相对抗跌，"
            f"{('、'.join(losers[:2]) or '暂无明确领跌')}相对走弱。"
        )
    else:
        headline = "午报：盘中板块数据不足，降级输出"
        summary = "上午指数/板块快照不可用，午报只保留自身时间边界内的事件。"

    return MiddaySeed(
        headline=headline,
        summary=summary,
        direction=_direction(change),
        confidence=0.50,
        risk_level=_severity(change),
        new_drivers=list(new_drivers)[:3],
        afternoon_opportunities=gainers[:3],
        afternoon_risks=losers[:3],
        risk_flags=[f"{name}午后延续走弱" for name in losers[:2]],
    )


def _sector_names(intraday: dict[str, object] | None, key: str) -> list[str]:
    if intraday is None:
        return []
    rows = _as_list(intraday.get(key)) or []
    names: list[str] = []
    for row in rows:
        info = _as_mapping(row)
        if info is None:
            continue
        name = _as_str(info.get("name")) or _as_str(info.get("sector"))
        if name is not None:
            names.append(name)
    return names


def _evening_seed(
    *,
    close_index: dict[str, object] | None,
    change: float | None,
    breadth: dict[str, object] | None,
    missing: Sequence[str],
) -> EveningSeed:
    name = _as_str(close_index.get("name")) if close_index is not None else None
    close_value = _as_float(close_index.get("close")) if close_index is not None else None
    if name is not None and change is not None:
        headline = f"晚报：{name}收报 {close_value if close_value is not None else 'NA'}（{change:+.2f}%）"
        summary = (
            f"收盘{name}涨跌 {change:+.2f}%；"
            "晚报直接引用当日市场溯源研判，不另起一套盘后归因。"
        )
    else:
        headline = "晚报：收盘快照不可用，降级输出"
        summary = "收盘行情缺失，晚报只保留可追溯的时间边界与降级说明。"

    risk_flags: list[str] = []
    if breadth is not None:
        advance = _as_int(breadth.get("advance_count"))
        decline = _as_int(breadth.get("decline_count"))
        if advance is not None and decline is not None and decline > advance:
            risk_flags.append("下跌家数多于上涨家数")
    return EveningSeed(
        headline=headline,
        summary=summary,
        direction=_direction(change),
        confidence=0.50,
        risk_level=_severity(change),
        errors_and_unresolved=[f"missing:{field}" for field in missing],
        next_day_verification=[],
        risk_flags=risk_flags,
    )


def _legacy_headline(
    report: dict[str, object] | None, status: str
) -> str:
    if report is not None:
        headline = _as_str(report.get("headline"))
        if headline is not None:
            return headline
    return f"legacy_review_{status}"


def _legacy_evidence_ids(report: dict[str, object] | None) -> list[str]:
    if report is None:
        return []
    raw = _as_list(report.get("evidence_ids")) or []
    return [item for item in raw if isinstance(item, str)]


def _evening_extras(close: dict[str, object] | None) -> dict[str, object] | None:
    """只读投影收盘行情；缺失时返回 None，不编造空壳行情。"""
    if close is None:
        return None
    indexes_payload = _as_mapping(close.get("indexes")) or {}
    indexes: list[dict[str, object]] = []
    first_change: float | None = None
    for code in sorted(indexes_payload):
        info = _as_mapping(indexes_payload[code])
        if info is None:
            continue
        change = _change_pct(info)
        if first_change is None:
            first_change = change
        indexes.append(
            {
                "name": _as_str(info.get("name")) or code,
                "close": _as_float(info.get("close")),
                "pct_change": change,
            }
        )
    breadth_info = _as_mapping(close.get("breadth"))
    breadth = (
        None
        if breadth_info is None
        else {
            "advance_count": _as_int(breadth_info.get("advance_count")),
            "decline_count": _as_int(breadth_info.get("decline_count")),
            "flat_count": _as_int(breadth_info.get("flat_count")),
        }
    )
    sectors_info = _as_mapping(close.get("sectors")) or {}
    sector_rows: list[tuple[str, float | None]] = []
    for name in sorted(sectors_info):
        info = _as_mapping(sectors_info[name])
        if info is not None:
            sector_rows.append((name, _change_pct(info)))
    ranked = sorted(
        sector_rows,
        key=lambda item: (item[1] is None, -(item[1] or 0.0), item[0]),
    )
    top_gainers = [
        {"name": name, "pct_change": change}
        for name, change in ranked
        if change is not None and change > 0
    ][:3]
    top_losers = [
        {"name": name, "pct_change": change}
        for name, change in sorted(
            (row for row in sector_rows if row[1] is not None and row[1] < 0),
            key=lambda item: (item[1] or 0.0, item[0]),
        )
    ][:3]
    advance = breadth.get("advance_count") if breadth is not None else None
    total_hint = _as_int(breadth_info.get("total_count")) if breadth_info else None
    has_anomaly = bool(
        (first_change is not None and abs(first_change) >= 1.5)
        or (
            isinstance(advance, int)
            and isinstance(total_hint, int)
            and total_hint > 0
            and advance / total_hint <= 0.3
        )
    )
    anomaly_conclusion = (
        f"指数涨跌 {first_change:+.2f}%，涨跌家数结构偏弱"
        if has_anomaly and first_change is not None
        else ("涨跌家数结构偏弱" if has_anomaly else None)
    )
    return {
        "has_anomaly": has_anomaly,
        "anomaly_conclusion": anomaly_conclusion,
        "indexes": indexes,
        "breadth": breadth,
        "sector_ranking": {
            "top_gainers": top_gainers,
            "top_losers": top_losers,
        },
    }
