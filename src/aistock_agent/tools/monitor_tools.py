"""个股监控工具 — 通过 Node.js /internal/monitor/* 获取研判资讯事件

包含两个工具：
- ``get_stock_monitor``：个股维度的研判资讯事件列表（数组型响应，用 ``get_list``）
- ``get_alert_history``：全局告警历史（分页，dict 型响应，用 ``get``）
"""

from datetime import timedelta

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call
from aistock_agent.utils.date import shanghai_today


@tool
@safe_tool_call
async def get_stock_monitor(symbol: str) -> str:
    """查询个股监控数据（该股票的研判资讯事件列表）

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get_list(f"/internal/monitor/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的监控数据"
    return _format_events(data, title=f"【{symbol}】监控事件")


@tool
@safe_tool_call
async def get_alert_history(symbol: str | None = None, days: int = 7) -> str:
    """查询全局告警历史（研判资讯事件，分页查询）

    Args:
        symbol: 可选股票代码，用于客户端过滤；不传则返回全局事件
        days: 查询天数（最近 N 天，内部换算 dateFrom 传给 Node）
    """
    # Node /internal/monitor/alerts 已弃用 days 参数（静默忽略），只认 dateFrom；
    # days 钳制 max(days,1)，按上海时区自然日换算 dateFrom=今天-days 天。
    # 补东八区后缀（与 Node 端既有消费先例 YYYY-MM-DDT00:00:00+08:00 一致）：
    # 纯日期 YYYY-MM-DD 的过滤语义依赖 DB session 时区，显式后缀消除时区漂移。
    date_from = (
        f"{(shanghai_today() - timedelta(days=max(days, 1))).isoformat()}T00:00:00+08:00"
    )
    data = await node_api.get(f"/internal/monitor/alerts?dateFrom={date_from}&limit=20&offset=0")
    if not data:
        return "暂无告警历史数据"
    events = data.get("events", [])
    if not isinstance(events, list) or not events:
        return "暂无告警历史数据"
    # symbol 客户端过滤：Node.js /monitor/alerts 是全局查询，不支持 symbol 参数
    filtered = [e for e in events if isinstance(e, dict)]
    if symbol:
        filtered = [
            e for e in filtered
            if e.get("stock_code") == symbol or e.get("symbol") == symbol
        ]
    return _format_events(filtered, title="告警历史")


def _format_events(events: list[dict[str, object]], *, title: str) -> str:
    """格式化研判资讯事件列表（MonitorEventItem[]）"""
    if not events:
        return f"{title}：暂无数据"

    lines: list[str] = [f"{title}："]
    for event in events[:10]:
        stock_name = event.get("stock_name", "-")
        change_type = event.get("change_type_name", event.get("change_type", ""))
        level = event.get("level", event.get("ai_impact", ""))
        ev_title = event.get("title", "无标题")
        ev_time = event.get("event_time", "")
        lines.append(f"  - [{ev_time}] {stock_name} [{change_type}/{level}] {ev_title}")
    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("general", get_stock_monitor)
register("general", get_alert_history)
register("alert", get_stock_monitor)
register("alert", get_alert_history)
