"""市场溯源事实快照构建服务 — 冻结事实、来源与主导现象。

本模块只做事实冻结和确定性主导现象识别，不调用 LLM，不输出因果判断。
因果归因由后续 Task 4 的 review agent 在 JSON 契约约束下完成。

设计要点：
- ``collect_global_market_facts`` 从 ``market_tools`` 导入，使 mock 路径
  ``aistock_agent.services.market_trace_snapshot.collect_global_market_facts``
  能拦截本模块内的本地引用。
- ``TavilyService`` 同理，mock 路径
  ``aistock_agent.services.market_trace_snapshot.TavilyService.search`` 可拦截。
- ``build_market_trace_snapshot`` 不 import 任何 Agent 模块，不调用 LLM。
- ``select_dominant_phenomenon`` 为纯函数，同输入同输出。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import structlog

from aistock_agent.schemas.market_trace import (
    DominantPhenomenon,
    MarketTraceSnapshot,
    SourceRecord,
)
from aistock_agent.services.data_client import node_api
from aistock_agent.services.tavily import TavilyService
from aistock_agent.tools.market_tools import collect_global_market_facts

logger = structlog.get_logger()


class MarketTraceSnapshotUnavailable(Exception):  # noqa: N818
    """当日 A 股收盘事实快照不可用。

    Node 返回 None、status 非 complete 或 coverage 不完整时抛出。
    不产出部分快照，由上层返回降级文本。

    命名保留 brief 中定义的 ``MarketTraceSnapshotUnavailable``（不带 Error
    后缀），与 Node.js 侧 ``MarketSnapshotUnavailableError`` 对应但非同一类。
    """


# ============================================================================
# 辅助函数
# ============================================================================


def _parse_yyyymmdd(date_str: object) -> datetime | None:
    """将 YYYYMMDD 字符串解析为 UTC datetime。"""
    if not isinstance(date_str, str) or len(date_str) != 8:
        return None
    try:
        return datetime.strptime(date_str, "%Y%m%d").replace(tzinfo=UTC)
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime | None:
    """从多种字符串格式解析 datetime，无时区时假设 UTC。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        cleaned = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt
    except (ValueError, TypeError):
        return None


def _safe_float(value: object, default: float = 0.0) -> float:
    """安全转换为 float。"""
    if isinstance(value, int | float):
        return float(value)
    return default


def _safe_int(value: object, default: int = 0) -> int:
    """安全转换为 int。"""
    if isinstance(value, int | float):
        return int(value)
    return default


def _safe_str(value: object, default: str = "") -> str:
    """安全转换为 str。"""
    if isinstance(value, str):
        return value
    if value is None:
        return default
    return str(value)


# ============================================================================
# 主导现象选择 — 纯函数，确定性打分
# ============================================================================


def select_dominant_phenomenon(
    a_share_facts: dict[str, object],
) -> DominantPhenomenon | None:
    """确定性主导现象识别。

    按 brief Step 5 打分表对 5 种 kind 打分：
    - 每种 kind 有 1 个基础信号（基础条件全部满足）和若干加分信号（各自独立）。
    - 信号总数 >= 2 时该 kind 成为候选。
    - 平局打破顺序：分数降序 → 绝对指数中位数降序 → 固定 kind 顺序。
    - 无候选时返回 None。

    本函数不决定原因，只决定需要调查的盘面异常。
    """
    indexes_raw = a_share_facts.get("indexes")
    if not isinstance(indexes_raw, list) or len(indexes_raw) < 6:
        return None

    # ── 提取 6 个指数的 pct_chg ──
    index_returns: dict[str, float] = {}
    for idx in indexes_raw:
        if not isinstance(idx, dict):
            continue
        ts_code = _safe_str(idx.get("ts_code"))
        pct_chg = idx.get("pct_chg")
        if ts_code and isinstance(pct_chg, int | float):
            index_returns[ts_code] = float(pct_chg)

    if len(index_returns) < 6:
        return None

    returns_list = list(index_returns.values())
    abs_returns_sorted = sorted(abs(r) for r in returns_list)
    # 6 个值的中位数 = (sorted[2] + sorted[3]) / 2
    index_median_abs = (abs_returns_sorted[2] + abs_returns_sorted[3]) / 2

    # 市场指数中位数（带符号）
    returns_sorted = sorted(returns_list)
    market_median = (returns_sorted[2] + returns_sorted[3]) / 2

    # ── 市场广度 ──
    breadth = a_share_facts.get("breadth")
    breadth_dict = breadth if isinstance(breadth, dict) else {}
    advance_ratio = _safe_float(breadth_dict.get("advance_ratio"))
    total_count = _safe_int(breadth_dict.get("total_count"))
    decline_count = _safe_int(breadth_dict.get("decline_count"))
    decline_ratio = decline_count / total_count if total_count > 0 else 0.0

    # ── 涨跌停 ──
    limits = a_share_facts.get("limits")
    limits_dict = limits if isinstance(limits, dict) else {}
    limit_up = _safe_int(limits_dict.get("up_count"))
    limit_down = _safe_int(limits_dict.get("down_count"))
    broken_count = _safe_int(limits_dict.get("broken_count"))
    highest_board = _safe_int(limits_dict.get("highest_board"))

    # ── 成交额 ──
    turnover = a_share_facts.get("turnover")
    turnover_dict = turnover if isinstance(turnover, dict) else {}
    turnover_change_pct = _safe_float(turnover_dict.get("change_pct"))

    # ── 板块 ──
    sectors = a_share_facts.get("sectors")
    sectors_dict = sectors if isinstance(sectors, dict) else {}
    top_gainers = sectors_dict.get("top_gainers")
    top_gainers_list = top_gainers if isinstance(top_gainers, list) else []
    top_losers = sectors_dict.get("top_losers")
    top_losers_list = top_losers if isinstance(top_losers, list) else []

    # ── 主力资金 ──
    main_force = a_share_facts.get("main_force")
    main_force_dict = main_force if isinstance(main_force, dict) else {}
    main_force_net = _safe_float(main_force_dict.get("large_and_extra_large_net_yuan"))

    # ── 6 个核心指数的 ts_code（用于 fact_ids）──
    index_fact_ids = [
        f"INDEX_{ts_code.replace('.', '_')}" for ts_code in index_returns
    ]

    # ── 逐 kind 打分 ──
    candidates: list[tuple[str, int, float]] = []
    # (kind, score, index_median_abs) — 平局打破用

    # --- broad_rally ---
    rally_count = sum(1 for r in returns_list if r >= 0.8)
    base_rally = rally_count >= 4 and advance_ratio >= 0.65
    bonus_rally_1 = limit_up >= limit_down + 20
    bonus_rally_2 = turnover_change_pct >= 10
    score_rally = (
        (1 if base_rally else 0)
        + (1 if bonus_rally_1 else 0)
        + (1 if bonus_rally_2 else 0)
    )
    if score_rally >= 2:
        candidates.append(("broad_rally", score_rally, index_median_abs))

    # --- broad_decline ---
    decline_idx_count = sum(1 for r in returns_list if r <= -0.8)
    base_decline = decline_idx_count >= 4 and decline_ratio >= 0.65
    bonus_decline_1 = limit_down >= limit_up + 20
    bonus_decline_2 = turnover_change_pct >= 10
    score_decline = (
        (1 if base_decline else 0)
        + (1 if bonus_decline_1 else 0)
        + (1 if bonus_decline_2 else 0)
    )
    if score_decline >= 2:
        candidates.append(("broad_decline", score_decline, index_median_abs))

    # --- style_divergence ---
    # 基础：任意两个核心指数收益率差绝对值 >= 1.5% 且方向相反
    returns_items = list(index_returns.values())
    base_divergence = False
    for i in range(len(returns_items)):
        for j in range(i + 1, len(returns_items)):
            r1 = returns_items[i]
            r2 = returns_items[j]
            if abs(r1 - r2) >= 1.5 and r1 * r2 < 0:
                base_divergence = True
                break
        if base_divergence:
            break
    # 加分 1：中证 1000 与沪深 300 方向相反
    csi1000 = index_returns.get("000852.SH", 0.0)
    csi300 = index_returns.get("000300.SH", 0.0)
    bonus_divergence_1 = csi1000 * csi300 < 0
    # 加分 2：板块涨跌前三与后三方向相反
    gainers_avg = (
        sum(_safe_float(s.get("pct_change")) for s in top_gainers_list[:3] if isinstance(s, dict))
        / max(len(top_gainers_list[:3]), 1)
    )
    losers_avg = (
        sum(_safe_float(s.get("pct_change")) for s in top_losers_list[:3] if isinstance(s, dict))
        / max(len(top_losers_list[:3]), 1)
    )
    bonus_divergence_2 = gainers_avg * losers_avg < 0
    score_divergence = (
        (1 if base_divergence else 0)
        + (1 if bonus_divergence_1 else 0)
        + (1 if bonus_divergence_2 else 0)
    )
    if score_divergence >= 2:
        candidates.append(("style_divergence", score_divergence, index_median_abs))

    # --- sector_concentration ---
    # 基础：最强或最弱概念板块绝对涨跌幅 >= 3%，且方向与大盘中位数相反
    strongest_pct = (
        _safe_float(top_gainers_list[0].get("pct_change"))
        if top_gainers_list and isinstance(top_gainers_list[0], dict)
        else 0.0
    )
    weakest_pct = (
        _safe_float(top_losers_list[0].get("pct_change"))
        if top_losers_list and isinstance(top_losers_list[0], dict)
        else 0.0
    )
    base_concentration = False
    concentration_direction = 0  # 1=正, -1=负
    if abs(strongest_pct) >= 3 and strongest_pct * market_median < 0:
        base_concentration = True
        concentration_direction = 1 if strongest_pct > 0 else -1
    elif abs(weakest_pct) >= 3 and weakest_pct * market_median < 0:
        base_concentration = True
        concentration_direction = 1 if weakest_pct > 0 else -1
    # 加分 1：该方向的前三板块净额同向
    if concentration_direction > 0:
        relevant = top_gainers_list[:3]
        bonus_concentration_1 = all(
            _safe_float(s.get("net_amount")) > 0
            for s in relevant
            if isinstance(s, dict)
        ) and len(relevant) > 0
    elif concentration_direction < 0:
        relevant = top_losers_list[:3]
        bonus_concentration_1 = all(
            _safe_float(s.get("net_amount")) < 0
            for s in relevant
            if isinstance(s, dict)
        ) and len(relevant) > 0
    else:
        bonus_concentration_1 = False
    # 加分 2：市场广度处于 0.40 到 0.60
    bonus_concentration_2 = 0.40 <= advance_ratio <= 0.60
    score_concentration = (
        (1 if base_concentration else 0)
        + (1 if bonus_concentration_1 else 0)
        + (1 if bonus_concentration_2 else 0)
    )
    if score_concentration >= 2:
        candidates.append(("sector_concentration", score_concentration, index_median_abs))

    # --- sentiment_extreme ---
    # 基础：涨停数 >= 50 或跌停数 >= 30，且炸板数 >= 涨停数的 0.35
    if limit_up > 0:
        base_sentiment = (limit_up >= 50 or limit_down >= 30) and (
            broken_count >= limit_up * 0.35
        )
    else:
        # limit_up == 0 时炸板条件平凡为真
        base_sentiment = limit_down >= 30
    # 加分 1：最高连板 >= 5
    bonus_sentiment_1 = highest_board >= 5
    # 加分 2：大单加特大单净额与指数方向一致
    bonus_sentiment_2 = (
        (market_median > 0 and main_force_net > 0)
        or (market_median < 0 and main_force_net < 0)
    )
    score_sentiment = (
        (1 if base_sentiment else 0)
        + (1 if bonus_sentiment_1 else 0)
        + (1 if bonus_sentiment_2 else 0)
    )
    if score_sentiment >= 2:
        candidates.append(("sentiment_extreme", score_sentiment, index_median_abs))

    # ── 无候选 → None ──
    if not candidates:
        return None

    # ── 平局打破：分数降序 → 绝对指数中位数降序 → 固定 kind 顺序 ──
    kind_order = [
        "broad_rally",
        "broad_decline",
        "style_divergence",
        "sector_concentration",
        "sentiment_extreme",
    ]
    candidates.sort(key=lambda c: (-c[1], -c[2], kind_order.index(c[0])))

    winning_kind, winning_score, _ = candidates[0]

    # ── 构建 fact_ids ──
    aggregate_by_kind: dict[str, list[str]] = {
        "broad_rally": ["BREADTH_ALL", "LIMITS_ALL", "TURNOVER_ALL"],
        "broad_decline": ["BREADTH_ALL", "LIMITS_ALL", "TURNOVER_ALL"],
        "style_divergence": ["SECTORS_ALL"],
        "sector_concentration": ["SECTORS_ALL", "BREADTH_ALL"],
        "sentiment_extreme": ["LIMITS_ALL", "MAIN_FORCE_ALL"],
    }
    fact_ids = index_fact_ids + aggregate_by_kind.get(winning_kind, [])

    summaries: dict[str, str] = {
        "broad_rally": "多个核心指数同步上涨，市场广度偏强",
        "broad_decline": "多个核心指数同步下跌，市场广度偏弱",
        "style_divergence": "核心指数方向背离，风格分化明显",
        "sector_concentration": "概念板块集中异动，与大盘方向相反",
        "sentiment_extreme": "涨跌停或炸板情绪指标极端",
    }

    return DominantPhenomenon(
        kind=winning_kind,
        summary=summaries[winning_kind],
        fact_ids=fact_ids,
        score=winning_score,
    )


# ============================================================================
# 快照构建
# ============================================================================


async def build_market_trace_snapshot(report_date: str) -> MarketTraceSnapshot:
    """构建市场溯源事实快照。

    顺序固定（brief Step 5）：
    1. 获取 Node 收盘快照；没有 status=complete 时立即失败。
    2. 以同一个 captured_at 收集境外行情、财联社快讯和两组固定 Tavily 检索。
    3. 将所有输入归一化为 SourceRecord，递增 source_id，不可用项写入 missing_fields。
    4. 只用 a_share 字段调用 select_dominant_phenomenon。
    5. 返回事实快照；不调用 LLM，不输出因果判断。
    """
    captured_at = datetime.now(UTC)

    # ── 1. 获取 Node 收盘快照 ──
    close_data = await node_api.get("/internal/market/close-snapshot")
    if close_data is None:
        raise MarketTraceSnapshotUnavailable(
            "Node close-snapshot returned None (market not closed or service unavailable)"
        )
    if close_data.get("status") != "complete":
        raise MarketTraceSnapshotUnavailable(
            f"Node close-snapshot status is not complete: {close_data.get('status')}"
        )
    coverage = close_data.get("coverage")
    coverage_dict = coverage if isinstance(coverage, dict) else {}
    current_daily = coverage_dict.get("current_daily")
    current_daily_dict = current_daily if isinstance(current_daily, dict) else {}
    if current_daily_dict.get("complete") is not True:
        raise MarketTraceSnapshotUnavailable(
            "Node close-snapshot coverage.current_daily.complete is not True"
        )

    # ── 2. 收集外部来源（同一 captured_at）──

    # 境外行情（同步函数，用 asyncio.to_thread 避免阻塞事件循环）
    try:
        global_facts = await asyncio.to_thread(collect_global_market_facts, captured_at)
    except Exception as e:
        logger.warning("collect_global_market_facts_failed", error=str(e))
        global_facts = []

    # 财联社最新快讯（Node /internal/news/latest，返回 dict 含 items 键）
    news_items: list[dict[str, object]] = []
    try:
        news_data = await node_api.get("/internal/news/latest")
        if isinstance(news_data, dict):
            raw_items = news_data.get("items", news_data.get("news", []))
            if isinstance(raw_items, list):
                news_items = [item for item in raw_items if isinstance(item, dict)]
    except Exception as e:
        logger.warning("cls_news_fetch_failed", error=str(e))

    # 两组固定 Tavily 检索
    tavily_query_1 = f"{report_date} 中国 资本市场 政策 产业 公告"
    tavily_query_2 = f"{report_date} 全球股市 利率 汇率 大宗商品 地缘风险"
    try:
        tavily_result_1 = TavilyService.search(
            query=tavily_query_1, topic="news", max_results=5
        )
    except Exception as e:
        logger.warning("tavily_search_1_failed", error=str(e))
        tavily_result_1 = {}
    try:
        tavily_result_2 = TavilyService.search(
            query=tavily_query_2, topic="news", max_results=5
        )
    except Exception as e:
        logger.warning("tavily_search_2_failed", error=str(e))
        tavily_result_2 = {}

    # ── 3. 归一化为 SourceRecord ──
    sources: dict[str, SourceRecord] = {}
    missing_fields: list[str] = []

    trade_date_node = _safe_str(close_data.get("trade_date"))
    trade_date_dt = _parse_yyyymmdd(trade_date_node)

    # A 股指数事实
    indexes_list = close_data.get("indexes")
    if isinstance(indexes_list, list):
        for idx in indexes_list:
            if not isinstance(idx, dict):
                continue
            ts_code = _safe_str(idx.get("ts_code"))
            if not ts_code:
                continue
            source_id = f"INDEX_{ts_code.replace('.', '_')}"
            sources[source_id] = SourceRecord(
                source_id=source_id,
                kind="market_fact",
                provider=_safe_str(idx.get("source"), "tushare:index_daily"),
                title=_safe_str(idx.get("name"), ts_code),
                content=(
                    f"trade_date={idx.get('trade_date')}, "
                    f"close={idx.get('close')}, "
                    f"pct_chg={idx.get('pct_chg')}, "
                    f"amount={idx.get('amount')}"
                ),
                url=None,
                occurred_at=trade_date_dt,
                captured_at=captured_at,
                source_level="market_data",
            )

    # A 股聚合事实（市场广度、成交额、涨跌停、主力资金、板块）
    if isinstance(breadth_dict := close_data.get("breadth"), dict) and breadth_dict:
        sources["BREADTH_ALL"] = SourceRecord(
            source_id="BREADTH_ALL",
            kind="market_fact",
            provider=_safe_str(breadth_dict.get("source"), "tushare:daily"),
            title="全市场涨跌家数",
            content=(
                f"total={breadth_dict.get('total_count')}, "
                f"advance={breadth_dict.get('advance_count')}, "
                f"decline={breadth_dict.get('decline_count')}, "
                f"flat={breadth_dict.get('flat_count')}, "
                f"advance_ratio={breadth_dict.get('advance_ratio')}"
            ),
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )

    if isinstance(turnover_dict := close_data.get("turnover"), dict) and turnover_dict:
        sources["TURNOVER_ALL"] = SourceRecord(
            source_id="TURNOVER_ALL",
            kind="market_fact",
            provider=_safe_str(turnover_dict.get("source"), "tushare:daily"),
            title="全市场成交额",
            content=(
                f"amount_yuan={turnover_dict.get('amount_yuan')}, "
                f"previous_amount_yuan={turnover_dict.get('previous_amount_yuan')}, "
                f"change_pct={turnover_dict.get('change_pct')}"
            ),
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )

    if isinstance(limits_dict := close_data.get("limits"), dict) and limits_dict:
        sources["LIMITS_ALL"] = SourceRecord(
            source_id="LIMITS_ALL",
            kind="market_fact",
            provider="tushare:limit_list_ths",
            title="涨跌停与连板统计",
            content=(
                f"up_count={limits_dict.get('up_count')}, "
                f"down_count={limits_dict.get('down_count')}, "
                f"broken_count={limits_dict.get('broken_count')}, "
                f"highest_board={limits_dict.get('highest_board')}"
            ),
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )

    if isinstance(main_force_dict := close_data.get("main_force"), dict) and main_force_dict:
        sources["MAIN_FORCE_ALL"] = SourceRecord(
            source_id="MAIN_FORCE_ALL",
            kind="market_fact",
            provider=_safe_str(main_force_dict.get("source"), "tushare:moneyflow_ths"),
            title="大单加特大单净额",
            content=(
                f"large_and_extra_large_net_yuan="
                f"{main_force_dict.get('large_and_extra_large_net_yuan')}"
            ),
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )

    if isinstance(sectors_dict := close_data.get("sectors"), dict) and sectors_dict:
        sources["SECTORS_ALL"] = SourceRecord(
            source_id="SECTORS_ALL",
            kind="market_fact",
            provider="tushare:moneyflow_cnt_ths",
            title="概念板块涨跌与资金流排序",
            content=(
                f"top_gainers_count={len(sectors_dict.get('top_gainers', []))}, "
                f"top_losers_count={len(sectors_dict.get('top_losers', []))}, "
                f"top_inflows_count={len(sectors_dict.get('top_inflows', []))}, "
                f"top_outflows_count={len(sectors_dict.get('top_outflows', []))}"
            ),
            url=None,
            occurred_at=trade_date_dt,
            captured_at=captured_at,
            source_level="market_data",
        )

    # 境外行情事实
    global_counter = 0
    for fact in global_facts:
        if not isinstance(fact, dict):
            continue
        global_counter += 1
        source_id = f"GLOBAL_{global_counter:03d}"
        occurred_at = _parse_datetime(fact.get("observed_at"))
        sources[source_id] = SourceRecord(
            source_id=source_id,
            kind="market_fact",
            provider="yfinance",
            title=_safe_str(fact.get("name"), _safe_str(fact.get("ticker"))),
            content=(
                f"ticker={fact.get('ticker')}, "
                f"price={fact.get('price')}, "
                f"change_pct={fact.get('change_pct')}, "
                f"observed_at={fact.get('observed_at')}"
            ),
            url=None,
            occurred_at=occurred_at,
            captured_at=captured_at,
            source_level="market_data",
        )
    if not global_facts:
        missing_fields.append("global_markets")

    # 财联社快讯（事件证据）
    news_counter = 0
    for item in news_items:
        time_str = item.get("time", item.get("ctime", ""))
        occurred_at = _parse_datetime(time_str)
        # 发生时间晚于 captured_at 的新闻不得进入快照
        if occurred_at is not None and occurred_at > captured_at:
            continue
        news_counter += 1
        source_id = f"NEWS_{news_counter:03d}"
        sources[source_id] = SourceRecord(
            source_id=source_id,
            kind="event_evidence",
            provider="cls",
            title=_safe_str(item.get("title"), "无标题"),
            content=_safe_str(item.get("brief", item.get("content", "")))[:500],
            url=item.get("url") if isinstance(item.get("url"), str) else None,
            occurred_at=occurred_at,
            captured_at=captured_at,
            source_level="reporting",
        )
    if not news_items:
        missing_fields.append("cls_news")

    # Tavily 检索结果（事件证据）
    search_counter = 0
    for label, tavily_result in [
        ("tavily_search_1", tavily_result_1),
        ("tavily_search_2", tavily_result_2),
    ]:
        results = tavily_result.get("results") if isinstance(tavily_result, dict) else None
        if not isinstance(results, list) or not results:
            missing_fields.append(label)
            continue
        for item in results:
            if not isinstance(item, dict):
                continue
            pub_date = item.get("published_date", item.get("publishedDate", ""))
            occurred_at = _parse_datetime(pub_date)
            # 发生时间晚于 captured_at 的检索结果不得进入快照
            if occurred_at is not None and occurred_at > captured_at:
                continue
            search_counter += 1
            source_id = f"SEARCH_{search_counter:03d}"
            sources[source_id] = SourceRecord(
                source_id=source_id,
                kind="event_evidence",
                provider="tavily",
                title=_safe_str(item.get("title"), "无标题"),
                content=_safe_str(item.get("content", ""))[:500],
                url=item.get("url") if isinstance(item.get("url"), str) else None,
                occurred_at=occurred_at,
                captured_at=captured_at,
                source_level="reporting",
            )

    # ── 4. 选择主导现象（只用 a_share 字段）──
    dominant = select_dominant_phenomenon(close_data)

    # ── 5. 返回事实快照 ──
    snapshot_id = f"trace-{trade_date_node}"

    return MarketTraceSnapshot(
        snapshot_id=snapshot_id,
        trade_date=report_date,
        captured_at=captured_at,
        a_share=close_data,
        sources=sources,
        missing_fields=missing_fields,
        dominant_phenomenon=dominant,
    )
