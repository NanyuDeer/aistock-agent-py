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
from aistock_agent.utils.date import CALENDAR_MAX_YEAR, CALENDAR_MIN_YEAR, add_trading_days

logger = logging.getLogger(__name__)

# 事件扫描窗口 = 含 target_date 当日共 ≤5 个交易日（§4.6）。
# add_trading_days 语义为"不含 d 向后推 n 个交易日"，故取 4 得 5 个交易日。
HORIZON_TRADING_DAYS = 4

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


async def load_event_window(
    target_date: str, horizon_days: int = HORIZON_TRADING_DAYS
) -> EventWindow:
    """自 target_date 起 ≤5 个交易日（含当日）的事件窗口。

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
    return EventWindow(events=events, high_events=high_events, source_missing=False)


async def load_earnings_density(date_from: str, date_to: str) -> list[dict[str, object]]:
    """存量披露密度（§4.2，仅存量已公告日，非未来预约，标注口径）。"""
    resp = await node_api.get(
        f"/internal/calendar/earnings-density?dateFrom={date_from}&dateTo={date_to}"
    )
    if not isinstance(resp, dict):
        return []
    density = resp.get("density")
    return list(density) if isinstance(density, list) else []
