"""sector_snapshot Skill — 板块强弱与风口数据。

仅读 Node /internal/leader/:tag_code 或 /internal/wind-leaders，
不调用 LLM、行业向量、市场 Trace、新闻。
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.skills.base import skill

_RE_SIX_DIGIT = re.compile(r"^\d{6}$")

_MAX_LEADERS = 5
_MAX_WIND_SECTORS = 8
_MAX_WIND_STOCKS = 3


def _is_six_digit_code(code: object) -> bool:
    return isinstance(code, str) and bool(_RE_SIX_DIGIT.match(code))


def _collect_symbols(data: dict[str, object], *, leaders_key: str) -> list[str]:
    """从 leader 或 main_stocks 列表中收集六位代码。"""
    symbols: list[str] = []
    raw = data.get(leaders_key)
    if not isinstance(raw, list):
        return symbols
    for item in raw:
        if not isinstance(item, dict):
            continue
        code = item.get("code")
        if _is_six_digit_code(code):
            symbols.append(code)
    return symbols


@skill
async def sector_snapshot(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    """板块强弱快照。

    Args:
        args: 支持 ``tag_code`` 可选字段。
        goal: 若 args 无 tag_code，fallback 到 ``goal.tag_codes[0]``。
    """
    tag_code = args.get("tag_code") or (goal.tag_codes[0] if goal.tag_codes else None)
    now = datetime.now(timezone.utc)  # noqa: UP017

    if tag_code:
        return await _handle_leader(tag_code, now)

    return await _handle_wind(now)


async def _handle_leader(tag_code: str, now: datetime) -> Evidence:
    """读取指定板块的龙头股。"""
    data = await node_api.get(f"/internal/leader/{tag_code}")
    if not data:
        raise ValueError(f"板块 {tag_code} 数据不可用")

    leaders_raw = data.get("leaders", [])
    if not isinstance(leaders_raw, list) or not leaders_raw:
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason=f"板块 {tag_code} 暂无龙头股数据",
            skill_name="sector_snapshot",
            raw={"tag_code": tag_code},
        )

    symbols: list[str] = []
    facts: list[str] = [f"板块龙头（{tag_code}）："]
    for stock in leaders_raw[:_MAX_LEADERS]:
        if not isinstance(stock, dict):
            continue
        name = stock.get("name", "-")
        code = stock.get("code", "-")
        change_pct = stock.get("change_pct", "-")
        reason = stock.get("reason", "")
        suffix = f" — {reason}" if reason else ""
        facts.append(f"  {name}({code}) 涨跌: {change_pct}%{suffix}")
        if _is_six_digit_code(code):
            symbols.append(code)

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"sector:leaders:{tag_code}",
                kind="realtime_quote",
                title=f"板块龙头 {tag_code}",
                snippet=facts[0] + " 共" + str(len(symbols)) + "只",
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=symbols,
        degraded=False,
        skill_name="sector_snapshot",
        raw={"tag_code": tag_code, "leader_count": len(symbols)},
    )


async def _handle_wind(now: datetime) -> Evidence:
    """读取风口龙头数据。"""
    data = await node_api.get("/internal/wind-leaders")
    if not data:
        raise ValueError("风口龙头数据不可用")

    sectors_raw = data.get("hot_sectors", [])
    if not isinstance(sectors_raw, list) or not sectors_raw:
        return Evidence(
            facts=[],
            sources=[],
            as_of=now,
            degraded=True,
            degraded_reason="暂无风口龙头数据",
            skill_name="sector_snapshot",
            raw={},
        )

    update_time = data.get("update_time", "")
    symbols: list[str] = []
    facts: list[str] = []
    header = f"风口龙头（更新: {update_time}）" if update_time else "风口龙头"
    facts.append(header)

    for sector in sectors_raw[:_MAX_WIND_SECTORS]:
        if not isinstance(sector, dict):
            continue
        name = sector.get("name", "未知板块")
        today_change = sector.get("today_change", "-")
        leading_stock = sector.get("leading_stock", "-")
        facts.append(f"  {name} 涨幅: {today_change}%  龙头: {leading_stock}")

        main_stocks = sector.get("main_stocks", [])
        if isinstance(main_stocks, list):
            for stock in main_stocks[:_MAX_WIND_STOCKS]:
                if not isinstance(stock, dict):
                    continue
                s_name = stock.get("name", "-")
                s_code = stock.get("code", "-")
                s_change = stock.get("change_pct", "-")
                facts.append(f"    - {s_name}({s_code}) 涨跌: {s_change}%")
                if _is_six_digit_code(s_code):
                    symbols.append(s_code)

    source_id = f"sector:wind:{update_time}" if update_time else "sector:wind"

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=source_id,
                kind="realtime_quote",
                title="风口龙头",
                snippet=f"共 {len(sectors_raw[: _MAX_WIND_SECTORS])} 个热门板块",
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=symbols,
        degraded=False,
        skill_name="sector_snapshot",
        raw={"update_time": update_time, "sector_count": len(symbols)},
    )
