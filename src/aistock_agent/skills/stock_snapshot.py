"""stock_snapshot Skill — 实时个股行情。

复用 tools/stock_tools.py 的 get_quote。非交易时段 / 数据源未返回 → degraded=True，
facts 含时段提示。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_quote
from aistock_agent.utils.date import trading_session_status

# get_quote / get_capital_flow 在数据为空时返回的固定字样
_EMPTY_MARKERS = ("未找到股票", "行情数据为空", "数据不可用")

# P11（线 3）：/internal/quote/:symbol 中文键 → 英文键映射（spec §3.1）。
# 防御性：键缺失时省略（Node core level 实际仅返回 股票代码/股票简称/最新价/涨跌幅/行情时间）。
_QUOTE_FIELD_MAP: dict[str, str] = {
    "股票简称": "name",
    "股票代码": "code",
    "最新价": "price",
    "涨跌额": "change",
    "涨跌幅": "change_pct",
    "市盈率": "pe",
    "市净率": "pb",
    "总市值": "market_cap",
}


def _build_quote_payload(data: dict[str, object]) -> dict[str, object] | None:
    """中文键行情 dict → 英文键 quote payload（供 raw.quote / cards 消费）。

    缺失字段省略、值 None 不写入；payload 为空（无任何可用键）时返回 None。
    get_quote 工具的文本输出契约不受影响（spec §3.1 冻结）。
    """
    payload: dict[str, object] = {}
    for cn_key, en_key in _QUOTE_FIELD_MAP.items():
        value = data.get(cn_key)
        if value is not None:
            payload[en_key] = value
    return payload or None


@skill
async def stock_snapshot(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("stock_snapshot requires 'symbol' in args or goal.symbols")

    quote_text = await get_quote.ainvoke({"symbol": symbol})
    # P11（线 3）：额外一次 /internal/quote 取结构化 dict 供 raw.quote（spec §3.1 允许；
    # 不改变 get_quote 工具文本契约）。node_api.get 失败返回 None 不抛（吞异常契约）。
    quote_data = await node_api.get(f"/internal/quote/{symbol}")
    quote_payload = _build_quote_payload(quote_data) if isinstance(quote_data, dict) else None
    now = datetime.now(UTC)

    # 判断数据有效性 + 交易时段
    status, hint = trading_session_status()
    is_empty = any(marker in quote_text for marker in _EMPTY_MARKERS)

    if is_empty:
        degraded = True
        reason = f"数据源未返回（{status}）"
        facts = [f"当前为 {hint}，{symbol} 实时行情暂未返回。"]
    elif status != "trading":
        degraded = True
        reason = f"非交易时段（{status}）"
        facts = [f"{hint}。以下为最近交易日数据：\n{quote_text}"]
    else:
        degraded = False
        reason = ""
        facts = [quote_text]

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"quote:{symbol}:{now.isoformat()}",
                kind="realtime_quote",
                title=f"{symbol} 实时行情",
                snippet=facts[0][:200],
                occurred_at=now,
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=degraded,
        degraded_reason=reason,
        skill_name="stock_snapshot",
        raw={
            "symbol": symbol,
            "trading_status": status,
            "quote": quote_payload,
        },
    )
