"""事件日历聚合（spec §4.1/§4.2/§4.6）。

- 消费 app-api `/internal/calendar/events`（L1 交割日 + market_calendar_events 合并，
  Python 侧不重复实现交割日计算）；
- 存量披露密度走 `/internal/calendar/earnings-density`（performance_reports 聚合，§4.2）；
- 空态区分（§4.6/G7）：空数组=正常无事件；接口失败(None)=数据源未接占位。
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import date

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import (
    CALENDAR_MAX_YEAR,
    CALENDAR_MIN_YEAR,
    add_trading_days,
    trading_days_between,
)

logger = logging.getLogger(__name__)

# 事件扫描窗口 = 含 target_date 当日共 ≤5 个交易日（§4.6）。
# add_trading_days 语义为"不含 d 向后推 n 个交易日"，故取 4 得 5 个交易日。
HORIZON_TRADING_DAYS = 4

# 展示窗口（需求 2：事件提前展示，不设 5 交易日上限）：horizon_days=None 时为全量，
# 即 target_date 起到 CALENDAR_MAX_YEAR 末（2026-12-31），越年自然截断（G3：截断留痕
# 而非 calendar_uncovered，避免前端误显"数据源未接入"）。
# 分析窗口（event_d/锚点/分支/certainty）必须维持 5 交易日口径，由
# split_analysis_window 从全量中切出，禁止直接用全量喂分析链路。
HORIZON_DISPLAY_YEAR_END_DAY = 31
HORIZON_DISPLAY_YEAR_END_MONTH = 12

# H3：必须逐字等于 app-api typeFromSource macro 正则词元，禁止各自维护
MACRO_EVENT_TERMS: tuple[str, ...] = ("发布日程", "CPI", "PPI", "PMI", "社融", "FOMC", "议息")
EVENT_TITLE_MAX_CHARS = 40
_COMPANY_TOKENS: tuple[str, ...] = (
    "股份", "科技", "集团", "有限公司", "银行", "证券", "医药", "公司",
)


def is_high_importance_event(title: str, source: str | None) -> bool:
    """三判据同时满足才升格 high（黑名单优先于白名单，spec §4.4）。

    P1 超长直接弃（禁止截断取前 N 字）；P2 含 6 位数字或公司主体词 → 否；
    P3 词元包含命中（L3 前瞻标题为「美联储 X 月议息会议」「美国 X 月 CPI 数据公布」
    等宏观日程句式，严格头/尾锚定会漏命中；防误升格由 P1/P2 承担）。
    """
    t = (title or "").strip()
    if not t or len(t) > EVENT_TITLE_MAX_CHARS:
        return False
    if re.search(r"\d{6}", t) or any(k in t for k in _COMPANY_TOKENS):
        return False
    return any(k in t for k in MACRO_EVENT_TERMS)


@dataclass
class EventWindow:
    events: list[dict[str, object]] = field(default_factory=list)
    high_events: list[dict[str, object]] = field(default_factory=list)
    source_missing: bool = False
    calendar_uncovered: bool = False
    # 展示窗口事件（2026-09-21，需求 2：事件提前展示，不限 5 交易日）。
    # 由 load_event_window(horizon_days=None) 设置；缺省空=该实例未做全量加载
    # （分析子集场景），消费端（_build_rhythm_card 投影 event_window）须回退 events。
    display_events: list[dict[str, object]] = field(default_factory=list)


async def load_event_window(
    target_date: str, horizon_days: int | None = HORIZON_TRADING_DAYS
) -> EventWindow:
    """自 target_date 起的未来事件窗口。

    horizon_days=None → **展示窗全量**：到 CALENDAR_MAX_YEAR 年末截断（G3：越年
    只截断不置 calendar_uncovered），供卡片 event_window 提前展示更远事件。
    horizon_days=4（默认）→ **分析窗**：含当日共 ≤5 个交易日，喂 event_confirm/
    event_d/锚点/分支（"临近=证据"语义，禁止误用全量）。

    §16 开放问题 6：target_date 年份超出 chinese_calendar 覆盖范围
    （CALENDAR_MIN_YEAR..CALENDAR_MAX_YEAR）→ 显式 fail-close
    （calendar_uncovered=True），不调 add_trading_days、不各自兜底，防语义分叉。
    """
    target = date.fromisoformat(target_date)
    if not CALENDAR_MIN_YEAR <= target.year <= CALENDAR_MAX_YEAR:
        logger.warning(
            "event_calendar.calendar_uncovered: target_date=%s "
            "(year=%d 超出 chinese_calendar 覆盖)",
            target_date,
            target.year,
        )
        return EventWindow(calendar_uncovered=True)
    if horizon_days is None:
        # 展示窗全量：到覆盖年末截断（G3），不调 add_trading_days（避免 ValueError）
        end = date(CALENDAR_MAX_YEAR, HORIZON_DISPLAY_YEAR_END_MONTH,
                   HORIZON_DISPLAY_YEAR_END_DAY)
    else:
        try:
            end = add_trading_days(target, horizon_days)
        except ValueError:  # 防御路径：horizon_days<0 等；年份越年已在上方显式拦截
            logger.warning("event_calendar.calendar_uncovered: target_date=%s", target_date)
            return EventWindow(calendar_uncovered=True)
    date_from, date_to = target_date, end.isoformat()
    raw = await node_api.get_calendar_events(date_from, date_to)
    if raw is None:
        return EventWindow(source_missing=True)
    events = list(raw)
    # 读取端 importance 归一（spec §4.4）：L3 前瞻入库 importance 恒 medium，
    # 命中宏观词元则升格 high；copy-on-write，不污染上游列表。下游
    # _event_confirm / build_event_branch / build_next_event_anchor 自动共享。
    normalized: list[dict[str, object]] = []
    for e in events:
        if e.get("source") == "L3" and e.get("importance") != "high" \
                and is_high_importance_event(str(e.get("title") or ""), "L3"):
            e = {**e, "importance": "high"}
        normalized.append(e)
    events = normalized
    high_events = [e for e in events if e.get("importance") == "high"]
    win = EventWindow(events=events, high_events=high_events, source_missing=False)
    if horizon_days is None:
        win.display_events = events
    return win


def split_analysis_window(
    events: list[dict[str, object]], target_date: str
) -> EventWindow:
    """从全量展示窗切出分析子窗（含 target 当日共 ≤5 个交易日）。

    A2（反方追打裁决）：子集口径必须按**交易日差**（trading_days_between <= 4），
    禁止自然日切片——否则周末后第 6/7 自然日事件被误纳入 event_d/锚点/分支，
    造成日数漂移。事件无日期/日期非法/越年 → 跳过（不抛异常，G6 纪律）。
    """
    target = date.fromisoformat(target_date)
    kept: list[dict[str, object]] = []
    for e in events:
        ev_date = str(e.get("date") or "")
        if not ev_date:
            continue
        try:
            d = trading_days_between(target, date.fromisoformat(ev_date))
        except ValueError:
            continue  # 坏日期跳过，不抛异常
        if d is None or d > HORIZON_TRADING_DAYS:
            continue  # 越年/超出 5 交易日 → 分析窗不取
        kept.append(e)
    high = [e for e in kept if e.get("importance") == "high"]
    return EventWindow(events=kept, high_events=high, display_events=events)


async def load_earnings_density(date_from: str, date_to: str) -> list[dict[str, object]]:
    """存量披露密度（§4.2，仅存量已公告日，非未来预约，标注口径）。"""
    resp = await node_api.get(
        f"/internal/calendar/earnings-density?dateFrom={date_from}&dateTo={date_to}"
    )
    if not isinstance(resp, dict):
        return []
    density = resp.get("density")
    return list(density) if isinstance(density, list) else []
