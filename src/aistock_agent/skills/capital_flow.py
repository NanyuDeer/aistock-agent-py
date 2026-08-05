"""capital_flow Skill — 个股资金流向。

复用 tools/stock_tools.py 的 get_capital_flow。非交易时段 / 数据源未返回 → degraded=True，
facts 含时段提示。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.services.data_client import node_api
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_capital_flow
from aistock_agent.utils.date import trading_session_status

_EMPTY_MARKERS = ("未找到股票", "资金流向数据为空", "数据不可用")

# P11（线 3）：/internal/flow/:symbol 新浪字段 → 英文键映射（r0_*=主力，netamount=净额，spec §3.1）。
# 源字段缺失省略；r0_* 为数值型，防御转换（新浪 Number||0 已保证数值；Tushare 兜底无 r0_* 键）。
_FLOW_FIELD_MAP: dict[str, str] = {
    "r0_in": "main_in",
    "r0_out": "main_out",
    "netamount": "net_amount",
}


def _build_flow_payload(data: dict[str, object]) -> dict[str, object] | None:
    """资金流向 dict → flow payload（供 raw.flow / cards 消费）。

    flow_5d 恒为空数组（源无 5 日柱状数据，spec §2.2）；数值字段防御转换。
    """
    payload: dict[str, object] = {}
    for src_key, en_key in _FLOW_FIELD_MAP.items():
        value = data.get(src_key)
        if value is None:
            continue
        try:
            payload[en_key] = float(value)
        except (TypeError, ValueError):
            continue
    payload["flow_5d"] = []
    return payload or None


@skill
async def capital_flow(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    symbol = args.get("symbol") or (goal.symbols[0] if goal.symbols else "")
    if not symbol:
        raise ValueError("capital_flow requires 'symbol' in args or goal.symbols")

    flow_text = await get_capital_flow.ainvoke({"symbol": symbol})
    # P11（线 3）：额外一次 /internal/flow 取结构化 dict 供 raw.flow（spec §3.1 允许；
    # 不改变 get_capital_flow 工具文本契约）。node_api.get 失败返回 None 不抛（吞异常契约）。
    flow_data = await node_api.get(f"/internal/flow/{symbol}")
    flow_payload = _build_flow_payload(flow_data) if isinstance(flow_data, dict) else None
    now = datetime.now(UTC)

    status, hint = trading_session_status()
    is_empty = any(marker in flow_text for marker in _EMPTY_MARKERS)

    if is_empty:
        degraded = True
        reason = f"数据源未返回（{status}）"
        facts = [f"当前为 {hint}，{symbol} 资金流向数据暂未返回。"]
    elif status != "trading":
        degraded = True
        reason = f"非交易时段（{status}）"
        facts = [f"{hint}。以下为最近交易日数据：\n{flow_text}"]
    else:
        degraded = False
        reason = ""
        facts = [flow_text]

    return Evidence(
        facts=facts,
        sources=[
            ChatSource(
                source_id=f"flow:{symbol}:{now.isoformat()}",
                kind="capital_flow",
                title=f"{symbol} 资金流向",
                snippet=facts[0][:200],
                captured_at=now,
            )
        ],
        as_of=now,
        symbols=[symbol],
        degraded=degraded,
        degraded_reason=reason,
        skill_name="capital_flow",
        raw={"symbol": symbol, "trading_status": status, "flow": flow_payload},
    )
