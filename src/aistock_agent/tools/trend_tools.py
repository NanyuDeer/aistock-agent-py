"""趋势股评分工具 — 通过 Node.js /internal/trend/* 获取评分数据

包含三个工具：
- ``get_trend_score``：个股趋势股评分（4维度百分制评分体系）
- ``get_trend_score_detail``：个股趋势股评分展开详情（含K线、新闻、政策趋势等）
- ``get_trend_top_stocks``：趋势股评分 Top 列表
"""

from langchain_core.tools import tool

from aistock_agent.services.data_client import node_api
from aistock_agent.tools.base import safe_tool_call


@tool
@safe_tool_call
async def get_trend_score(symbol: str) -> str:
    """查询个股趋势股评分（4维度百分制评分体系：技术面35%+赛道25%+消息20%+基本面20%）

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get(f"/internal/trend/score/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的趋势股评分数据"
    return _format_score(data)


@tool
@safe_tool_call
async def get_trend_score_detail(symbol: str) -> str:
    """查询个股趋势股评分展开详情（含K线指标、概念板块K线、新闻、政策趋势等）

    Args:
        symbol: 6位股票代码，如 600519
    """
    data = await node_api.get(f"/internal/trend/score/{symbol}/detail")
    if not data:
        return f"未找到股票 {symbol} 的趋势股评分详情"
    return _format_detail(data)


@tool
@safe_tool_call
async def get_trend_top_stocks(limit: int = 20) -> str:
    """查询趋势股评分 Top 列表（按总分降序，排除D级）

    Args:
        limit: 返回数量，默认20
    """
    data = await node_api.get_list(f"/internal/trend/top?limit={limit}")
    if not data:
        return "暂无趋势股 Top 列表数据"
    return _format_top_stocks(data)


def _format_score(data: dict[str, object]) -> str:
    """格式化趋势股评分结果（TrendScoreResult）"""
    score = data.get("score", "-")
    label = data.get("label", "-")
    expected = data.get("expectedMultiple", "-")
    description = str(data.get("description", ""))
    ai_conclusion = str(data.get("aiConclusion", ""))

    lines: list[str] = [f"趋势股评分: {score}分（{label}级）  预期倍数: {expected}"]
    if ai_conclusion:
        lines.append(f"  AI结论: {ai_conclusion}")
    if description:
        lines.append(f"  {description}")

    dimensions = data.get("dimensions", [])
    if isinstance(dimensions, list):
        lines.append("  维度明细：")
        for dim in dimensions:
            if not isinstance(dim, dict):
                continue
            d_name = dim.get("name", "-")
            d_weight = dim.get("weight", "-")
            d_score = dim.get("score", "-")
            lines.append(f"    - {d_name}({d_weight}%): {d_score}分")
    return "\n".join(lines)


def _format_detail(data: dict[str, object]) -> str:
    """格式化趋势股评分展开详情"""
    score = data.get("score", "-")
    label = data.get("label", "-")
    expected = data.get("expectedMultiple", "-")
    lines: list[str] = [f"趋势股评分详情: {score}分（{label}级）  预期倍数: {expected}"]

    dimensions = data.get("dimensions", [])
    if not isinstance(dimensions, list):
        return "\n".join(lines)

    for dim in dimensions:
        if not isinstance(dim, dict):
            continue
        d_name = dim.get("name", "-")
        d_weight = dim.get("weight", "-")
        d_score = dim.get("score", "-")
        lines.append(f"\n【{d_name}】权重={d_weight}%  得分={d_score}")

        detail = dim.get("detail", {})
        if not isinstance(detail, dict):
            continue

        if d_name == "技术面":
            indicators = detail.get("indicators", {})
            if isinstance(indicators, dict):
                lines.append(f"  低点以来涨幅: {indicators.get('lowPointGain', '-')}%")
                lines.append(f"  60日线位置: {indicators.get('ma60Position', '-')}  趋势: {indicators.get('ma60Trend', '-')}")
                lines.append(f"  250日新高: {indicators.get('isNewHigh250', '-')}  120日新高: {indicators.get('isNewHigh120', '-')}")
                lines.append(f"  最大回撤: {indicators.get('maxDrawdown', '-')}%")
            # 龙头股加成
            is_leader = detail.get("isLeader", False)
            leader_board_name = detail.get("leaderBoardName", "")
            if is_leader:
                lines.append(f"  龙头股加成: 是(+8分){f'  最佳板块龙头: {leader_board_name}' if leader_board_name else ''}")
            else:
                lines.append("  龙头股加成: 否")
            concept = detail.get("conceptKline", {})
            if isinstance(concept, dict):
                lines.append(f"  概念板块: {concept.get('name', '-')}")

        elif d_name == "行业赛道景气":
            lines.append(f"  最佳概念板块: {detail.get('sectorName', '-')}")
            lines.append(f"  60日上榜次数: {detail.get('sectorListCount60d', '-')}")
            lines.append(f"  板块月涨幅: {detail.get('sectorStrength', '-')}")
            weekly = detail.get("weeklyListingTrend", [])
            if isinstance(weekly, list) and weekly:
                lines.append(f"  周度上榜趋势: {weekly}")
            policy_items = detail.get("policyItems", [])
            if isinstance(policy_items, list):
                for item in policy_items[:3]:
                    if isinstance(item, dict):
                        lines.append(f"  政策: {item.get('name', '-')} - {item.get('desc', '-')}")

        elif d_name == "消息面催化":
            lines.append(f"  调研家数: {detail.get('researchCount', '-')}")
            lines.append(f"  硬催化: {detail.get('hardCatalyst', '-')}")
            news = detail.get("news", [])
            if isinstance(news, list):
                for item in news[:5]:
                    if isinstance(item, dict):
                        lines.append(f"  新闻: {item.get('title', '-')} ({item.get('publishTime', '-')})")

        elif d_name == "基本面":
            sub_dims = detail.get("subDimensions", [])
            if isinstance(sub_dims, list):
                for sub in sub_dims:
                    if isinstance(sub, dict):
                        lines.append(f"  {sub.get('name', '-')}({sub.get('weight', '-')}%): {sub.get('score', '-')}分")

    return "\n".join(lines)


def _format_top_stocks(data: list[dict[str, object]]) -> str:
    """格式化趋势股 Top 列表"""
    if not data:
        return "暂无趋势股 Top 列表数据"

    lines: list[str] = ["趋势股 Top 列表："]
    for i, stock in enumerate(data[:20], 1):
        if not isinstance(stock, dict):
            continue
        name = stock.get("name", "-")
        code = stock.get("symbol", "-")
        score = stock.get("score", "-")
        label = stock.get("label", "-")
        expected = stock.get("expectedMultiple", "-")
        industry = stock.get("industry", "-")
        lines.append(f"  {i}. {name}({code})  评分: {score}  等级: {label}  预期: {expected}  行业: {industry}")
    return "\n".join(lines)


# ── 自注册到 Tool Registry ──────────────────────────────────────────
from aistock_agent.tools.registry import register  # noqa: E402

register("trend_score", get_trend_score)
register("trend_score", get_trend_score_detail)
register("trend_score", get_trend_top_stocks)

# 同时注册到 general category，供 ai_advisor_agent 降级使用
register("general", get_trend_score)
register("general", get_trend_top_stocks)
