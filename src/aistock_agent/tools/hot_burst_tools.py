"""机构调研推荐热门股工具 — 通过 Node.js /internal/institution-research* 获取

包含两个工具：
- ``get_hot_burst``：机构调研推荐热门股检测结果（四信号源共振模型）
- ``get_hot_burst_history``：历史检测记录（从数据库分页查询）
"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def get_hot_burst(
    hours: int = 18,
    min_resonance_count: int = 2,
    limit: int = 20,
) -> str:
    """查询机构调研推荐热门股检测结果（四信号源共振模型），用于热点挖掘"""
    safe_hours = min(max(hours, 1), 72)
    safe_min_resonance = min(max(min_resonance_count, 0), 4)
    safe_limit = min(max(limit, 1), 100)
    data = await node_api.get(
        f"/internal/institution-research?hours={safe_hours}"
        f"&min_resonance_count={safe_min_resonance}&limit={safe_limit}"
    )
    if not data:
        return "暂无机构调研推荐热门股数据"
    return _format_hot_burst(data)


@tool
@safe_tool_call
async def get_hot_burst_history(
    limit: int = 50,
    min_resonance_only: bool = True,
    days: int = 30,
    offset: int = 0,
) -> str:
    """查询机构调研热门股历史记录。"""
    safe_limit = min(max(limit, 1), 200)
    safe_days = min(max(days, 1), 365)
    safe_offset = max(offset, 0)
    flag = str(min_resonance_only).lower()
    data = await node_api.get(
        "/internal/institution-research/history"
        f"?limit={safe_limit}&min_resonance_only={flag}&days={safe_days}&offset={safe_offset}"
    )
    if not data:
        return "暂无机构调研热门股历史记录"
    return _format_history(data)


def _format_hot_burst(data: dict[str, object]) -> str:
    """格式化机构调研推荐热门股检测结果（HotBurstResult）"""
    update_time = data.get("update_time", "-")
    total_checked = data.get("total_stocks_checked", 0)
    resonance_count = data.get("resonance_count", 0)
    outbreaks = data.get("outbreaks", [])

    lines = [
        f"机构调研推荐热门股更新时间: {update_time}",
        f"共振信号数量: {resonance_count} / 扫描股票数: {total_checked}",
    ]

    if not isinstance(outbreaks, list) or not outbreaks:
        lines.append("当前没有命中的机构调研热门股。")
        return "\n".join(lines)

    lines.append("重点个股:")
    for item in outbreaks[:20]:
        if not isinstance(item, dict):
            continue
        stock_name = item.get("stockName", item.get("stock_name", "-"))
        symbol = item.get("symbol", "-")
        level = item.get("resonanceLevel", item.get("resonance_level", "-"))
        score = item.get("resonanceScore", item.get("resonance_score", "-"))
        sector = item.get("sectorInfo", item.get("sector_info", "-"))
        price = item.get("price", "-")
        change_pct = item.get("changePct", item.get("change_pct", "-"))
        resonance = item.get("resonanceCount", item.get("resonance_count", "-"))

        trigger_tags_raw = item.get("triggerTags", item.get("keywords", []))
        if isinstance(trigger_tags_raw, list):
            trigger_tags = "、".join(str(tag) for tag in trigger_tags_raw[:4] if tag)
        else:
            trigger_tags = str(trigger_tags_raw)

        lines.append(
            f"- {stock_name}({symbol}) | 等级={level} | 分数={score} | 共振源={resonance} | "
            f"价格={price} | 涨跌幅={change_pct} | 板块={sector} | 关键词={trigger_tags or '-'}"
        )
    return "\n".join(lines)


def _format_history(data: dict[str, object]) -> str:
    total = data.get("total", 0)
    records = data.get("records", [])
    lines = [f"机构调研热门股历史记录总数: {total}"]

    if not isinstance(records, list) or not records:
        lines.append("暂无历史记录。")
        return "\n".join(lines)

    for item in records[:20]:
        if not isinstance(item, dict):
            continue
        stock_name = item.get("stock_name", item.get("stockName", "-"))
        symbol = item.get("symbol", item.get("stock_code", "-"))
        score = item.get("resonance_score", item.get("resonanceScore", "-"))
        level = item.get("resonance_level", item.get("resonanceLevel", "-"))
        detected_at = item.get("detected_at", item.get("detectedAt", item.get("push_date", "-")))
        keywords = item.get("keywords", item.get("theme", "-"))
        lines.append(
            f"- {detected_at} | {stock_name}({symbol}) | 等级={level}"
            f" | 分数={score} | 关键词={keywords}"
        )
    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("hot_burst", get_hot_burst)
register("hot_burst", get_hot_burst_history)
register("general", get_hot_burst)
register("general", get_hot_burst_history)
# advisor agent 复用
register("advisor", get_hot_burst)
register("advisor", get_hot_burst_history)
