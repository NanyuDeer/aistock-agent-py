"""未来事件影响板块预计算（重大事件时间线 impact_sectors，2026-09-24）。

数据流（时间线影响板块改造方案）：
    Event Entity（calendar 未来事件）→ title+detail → 行业向量（KG 语义）匹配
    → 相似度≥阈值 Top3 → POST /internal/event-entities 幂等写回（不新增第二张板块表）。

语义边界：
- 只处理 `source_type='calendar'` 且 `status∈{scheduled,upcoming}` 且 `impact_sectors`
  为空的未来事件；**已有事件传导结果的事件由时间线读取 chain 优先展示**（优先级：
  conduction chain Top3 > 本列 > 空），本列仅为无传导结果的未来事件兜底。
- 宏观/利率类未来事件（如「美联储议息会议」）匹配不到可靠行业 → 保持 []（方案 A：
  空不展示，**绝不 LLM 强猜**）。
- 幂等守卫：只处理 impact_sectors 为空的事件，非空即跳过；空结果不写回（默认已是 []），
  下次运行自然重试（embedding 匹配廉价，无 LLM）。`updated_at` 节流不可靠——app-api
  Calendar 物化 cron 每日重新 upsert 会刷新 updated_at，故以「空才处理」作为节流。
- 失败容错：单行失败 log 后继续，不中断整批；抓取/传导主链路不受影响（独立 cron）。
"""

import structlog
from datetime import timedelta

from aistock_agent.config import settings
from aistock_agent.services.data_client import node_api
from aistock_agent.tools.industry_vector_search import semantic_match_industries
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

# 写回时间线的最大板块数（对齐前端展示：最多 3 个，超出 +N）
IMPACT_SECTORS_TOP_N = 3
# 行业向量匹配相似度阈值（对齐 semantic_match_industries 默认 0.7；低于阈值视为无可靠行业）
IMPACT_SECTORS_MATCH_THRESHOLD = 0.7
# 预计算窗口：今天 → 未来 N 天内的未来事件
PRECOMPUTE_WINDOW_DAYS = 90
# 仅处理未来事件（scheduled/upcoming），已发生/进行中的事件由传导 chain 主导
_ELIGIBLE_STATUSES = frozenset({"scheduled", "upcoming"})


def _non_empty_sectors(value: object) -> bool:
    """impact_sectors 列是否已有内容（JSONB 解析后为 list[str]；非法/缺失视为空）。"""
    return isinstance(value, list) and any(
        isinstance(v, str) and v.strip() for v in value
    )


def _eligible_for_precompute(row: dict[str, object]) -> bool:
    """预计算资格：calendar 未来事件且 impact_sectors 为空。

    已发生/进行中事件、新闻/公告等其他来源事件、已有板块结果的事件一律跳过。
    """
    if not isinstance(row, dict):
        return False
    if str(row.get("source_type") or "") != "calendar":
        return False
    if str(row.get("event_status") or "") not in _ELIGIBLE_STATUSES:
        return False
    return not _non_empty_sectors(row.get("impact_sectors"))


def _select_top_industries(industries: list[dict[str, object]] | None, top_n: int = IMPACT_SECTORS_TOP_N) -> list[str]:
    """从语义匹配结果取 TopN 行业名（按 similarity 降序，去重，保序）。

    输入来自 semantic_match_industries（[{name, similarity, ...}]，pgvector 返回）。
    """
    entries: list[tuple[str, float]] = []
    for ind in industries or []:
        if not isinstance(ind, dict):
            continue
        name = str(ind.get("name", "")).strip()
        if not name:
            continue
        try:
            similarity = float(str(ind.get("similarity", 0)))
        except (TypeError, ValueError):
            similarity = 0.0
        entries.append((name, similarity))
    entries.sort(key=lambda item: item[1], reverse=True)
    seen: set[str] = set()
    out: list[str] = []
    for name, _similarity in entries[:top_n]:
        if name not in seen:
            seen.add(name)
            out.append(name)
    return out


async def run_impact_sectors_precompute() -> None:
    """独立 cron 作业入口：扫描未来 calendar 事件并预生成影响板块写回。

    任一步骤失败只告警，绝不阻断主链路（由 scheduler 独立注册、独立重试）。
    """
    if not settings.impact_sectors_precompute_enabled:
        logger.info("impact_sectors_precompute_disabled")
        return

    today = shanghai_today()
    params = {
        "dateFrom": today.isoformat(),
        "dateTo": (today + timedelta(days=PRECOMPUTE_WINDOW_DAYS)).isoformat(),
    }
    rows = await node_api.get_event_entities(params)
    if rows is None:
        logger.warning("impact_sectors_precompute_fetch_failed")
        return

    eligible = [r for r in rows if _eligible_for_precompute(r)]
    logger.info(
        "impact_sectors_precompute_start",
        total=len(rows),
        eligible=len(eligible),
    )

    updated = 0
    for row in eligible:
        try:
            title = str(row.get("title", "")).strip()
            summary = str(row.get("summary") or "").strip()
            query_text = f"{title} {summary}".strip()
            if not query_text:
                continue
            # 标题+摘要整体做语义匹配（宏观事件自然落空 → 保持 []，方案 A）
            industries = await semantic_match_industries(
                [query_text],
                threshold=IMPACT_SECTORS_MATCH_THRESHOLD,
                limit=IMPACT_SECTORS_TOP_N * 3,
            )
            sectors = _select_top_industries(industries)
            if not sectors:
                # 无可靠行业 → 不写回（默认已是 []），下次运行自然重试
                logger.info(
                    "impact_sectors_precompute_empty",
                    event_id=str(row.get("event_id", "")),
                    title=title[:50],
                )
                continue
            body: dict[str, object] = {
                "title": title,
                "source_type": str(row.get("source_type", "calendar")),
                "event_start_time": str(row.get("event_start_time", "")),
                "time_source": str(row.get("time_source", "calendar")),
                "time_confidence": row.get("time_confidence"),
                "summary": row.get("summary"),
                "source_event_id": row.get("source_event_id"),
                "impact_sectors": sectors,
            }
            result = await node_api.post_event_entity(body)
            if result:
                updated += 1
                logger.info(
                    "impact_sectors_precompute_written",
                    event_id=result.get("event_id"),
                    sectors=sectors,
                )
        except Exception:  # noqa: BLE001 — 单行失败不中断整批
            logger.exception(
                "impact_sectors_precompute_row_failed",
                event_id=str(row.get("event_id", "")),
            )
            continue

    logger.info("impact_sectors_precompute_done", updated=updated)
