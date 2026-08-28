"""业绩报告工具 — 通过 Node.js /internal/* API 获取业绩报告数据

供 AI 对话回答"XX 公司的业绩报告/财报/业绩快报"及"最新业绩报告有哪些"类问题。
注册分类：stock（个股分析 worker）、general（兜底对话）。
"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call
from aistock_agent.tools.registry import register


def _to_yi(value: object) -> str:
    """元 → 亿元字符串（空值返回 '—'）"""
    if value is None or value == "":
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{num / 1e8:.2f} 亿元"


def _to_num(value: object, digits: int = 2) -> str:
    """数值格式化（空值返回 '—'）"""
    if value is None or value == "":
        return "—"
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    return f"{num:.{digits}f}"


def _format_performance_report(data: dict[str, object]) -> str:
    """格式化个股业绩报告（多期列表）"""
    name = data.get("stock_name") or "未知"
    symbol = data.get("symbol") or ""
    reports = data.get("reports")
    if not isinstance(reports, list) or len(reports) == 0:
        return f"未找到 {name}（{symbol}）的业绩报告数据"

    lines = [f"【{name}】（{symbol}）业绩报告"]
    for idx, rep in enumerate(reports, start=1):
        if not isinstance(rep, dict):
            continue
        end_date = rep.get("end_date") or ""
        period = (
            f"{end_date[:4]}半年报"
            if end_date[4:6] == "06"
            else f"{end_date[:4]}一季报"
            if end_date[4:6] == "03"
            else f"{end_date[:4]}三季报"
            if end_date[4:6] == "09"
            else f"{end_date[:4]}年报"
            if end_date[4:6] == "12"
            else (end_date or "—")
        )
        label = rep.get("report_type_label") or "业绩报告"
        ann_date = rep.get("ann_date") or "—"
        ai_tag = rep.get("ai_tag") or ""

        lines.append(
            f"{idx}. {period} · {label}（公告日 {ann_date}）\n"
            f"   营业总收入: {_to_yi(rep.get('total_revenue'))}\n"
            f"   归母净利润: {_to_yi(rep.get('n_income_attr_p'))}\n"
            f"   基本每股收益: {_to_num(rep.get('basic_eps'))} 元"
        )
        if ai_tag:
            lines.append(f"   AI研判: {ai_tag}")

    return "\n".join(lines)


def _format_latest_reports(data: dict[str, object]) -> str:
    """格式化最新披露业绩报告列表"""
    reports = data.get("reports")
    if not isinstance(reports, list) or len(reports) == 0:
        return "暂无最新披露的业绩报告数据"

    report_type_label = "业绩报告"
    if reports and isinstance(reports[0], dict):
        label = reports[0].get("report_type_label")
        if label:
            report_type_label = label

    lines = [f"最新披露的{report_type_label}如下："]
    for idx, rep in enumerate(reports, start=1):
        if not isinstance(rep, dict):
            continue
        name = rep.get("stock_name") or ""
        symbol = rep.get("symbol") or ""
        ann_date = rep.get("ann_date") or "—"
        revenue = _to_yi(rep.get("total_revenue"))
        profit = _to_yi(rep.get("n_income_attr_p"))
        revenue_yoy = rep.get("revenue_yoy")
        profit_yoy = rep.get("profit_yoy")
        yoy_str = ""
        if profit_yoy is not None and profit_yoy != "":
            yoy_str = f" 净利同比 {float(profit_yoy):+.1f}%"
        elif revenue_yoy is not None and revenue_yoy != "":
            yoy_str = f" 营收同比 {float(revenue_yoy):+.1f}%"

        lines.append(
            f"{idx}. {name}（{symbol}）公告日 {ann_date}\n"
            f"   营业总收入: {revenue}  归母净利润: {profit}{yoy_str}"
        )

    return "\n".join(lines)


@tool
@safe_tool_call
async def get_performance_report(symbol: str) -> str:
    """查询个股业绩报告（正式报告与业绩快报）

    Args:
        symbol: 6位股票代码，如 600519（贵州茅台）
    """
    data = await node_api.get(f"/internal/performance-report/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的业绩报告数据"
    return _format_performance_report(data)


@tool
@safe_tool_call
async def get_latest_performance_reports(report_type: str = "formal", limit: int = 10) -> str:
    """查询最新披露的业绩报告列表

    Args:
        report_type: 报告类型，formal（正式报告）/ express（业绩快报）/ all（全部），默认 formal
        limit: 返回条数，默认 10，最大 30
    """
    data = await node_api.get(
        f"/internal/performance-reports/latest?reportType={report_type}&limit={limit}"
    )
    if not data:
        return "最新业绩报告数据暂不可用"
    return _format_latest_reports(data)


# ── 注册到工具注册中心（registry）─────────────────────────────────
register("stock", get_performance_report)
register("general", get_performance_report)
register("general", get_latest_performance_reports)
