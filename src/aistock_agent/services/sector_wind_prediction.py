"""每日长线风口板块批量预判任务（板块四环前端 spec §6.3）。

收盘后对 leaders 页前端可见的风口板块批量产出板块预判（为页面"预判-验证"数据
闭环供给数据）：拉取 Node hot-sectors 双榜并集（长线/短线两档各 top8，Node 侧
applyDualRankings 已排序，取 ≤16 条且 cycle != 'none'），逐板块：
resolve 归一（ths）→ 主因板块排除 → 幂等检查 → predict_sector 落库
（source_type="sector_prediction"，source_id=`sector:{resolve后权威中文名}:{date}`，
status 默认 pending → 16:00 到期验证 → 画像/迭代样本）。

与"大盘溯源主因板块级联预判"（review_done → sector_trace →
_cascade_sector_prediction，event_consumers.py）的关系：主因板块当日已由级联链路
预判并扣费，本任务读当日 sector_trace 报告（display_report.sectors 主因板块中文名）
resolve 成 ts_code 集合作排除，避免同日同板块重复扣费；list_predictions(source_id)
幂等检查兜底其余重复场景（Node upsert 会覆盖 prediction 并重置 status=pending，
禁止裸覆盖已落库/已验证记录）。

失败容忍：resolve 失败 / 单板块异常仅 warning 并 continue（宁缺毋滥）；数据缺失、
报告读取失败按"无候选 / 无主因（不排除）"处理；本模块不向调度器抛异常。
"""

import structlog

from aistock_agent.services.data_client import node_api
from aistock_agent.services.prediction_service import predict_sector
from aistock_agent.services.prediction_targets import resolve_sector_target
from aistock_agent.utils.date import shanghai_today

logger = structlog.get_logger()

# Node hot-sectors 双榜并集上限：长线 top8 + 短线 top8（both 去重后 ≤16）
_WIND_LEADERS_LIMIT = 16
_WIND_LEADERS_PATH = f"/internal/wind-leaders?limit={_WIND_LEADERS_LIMIT}"

# 主因板块级联预判落库的报告类型（display_report.sectors = [板块中文名]）
_SECTOR_TRACE_REPORT_TYPE = "sector_trace"


def _to_float(value: object) -> float | None:
    """数值字段容忍转换（int/float/可解析 str）→ float；None/非数值 → None。

    风口行情字段（today_change/amount/freq20/score）可能为 None 或 "—" 等
    占位串，快照仅收录真实数值（宁缺毋滥，不把脏数据喂给预判输入）。
    """
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        stripped = value.strip()
        try:
            return float(stripped)
        except ValueError:
            return None
    return None


def _build_sector_snapshot(
    row: dict[str, object], resolved_name: str
) -> dict[str, object]:
    """构造轻量 sector_snapshot（风口行情事实，仅含非 None 字段，提升预判质量）。

    结构自由 dict：sector 块带 resolve 后权威名与行情事实（pct_change/amount/
    lead_stock/cycle/freq20），meta 标注数据来源 wind_leader，供 LLM 参考与溯源。
    """
    sector_fact: dict[str, object] = {}
    today_change = _to_float(row.get("today_change"))
    if today_change is not None:
        sector_fact["pct_change"] = today_change
    amount = _to_float(row.get("amount"))
    if amount is not None:
        sector_fact["amount"] = amount
    leading_stock = row.get("leading_stock")
    if isinstance(leading_stock, str) and leading_stock.strip():
        sector_fact["lead_stock"] = leading_stock.strip()
    cycle = row.get("cycle")
    if isinstance(cycle, str) and cycle in ("long", "short", "both"):
        sector_fact["cycle"] = cycle
    freq20 = _to_float(row.get("freq20"))
    if freq20 is not None:
        sector_fact["freq20"] = freq20
    return {
        "sector": {"name": resolved_name, **sector_fact},
        "meta": {"source": "wind_leader"},
    }


def _filter_wind_sectors(data: dict[str, object]) -> list[dict[str, object]]:
    """风口板块候选提取：cycle != 'none' 且 name 非空，按序去重取前 ≤16。

    Node 侧 getAnalysis 输出的 hot_sectors 已是 applyDualRankings 排序
    （长线 top8 在前、短线补后、'none' 追加末尾），此处仅做防御性过滤去重，
    顺序保持不变，得到"长线/短线两档 top8 并集"候选。
    """
    raw = data.get("hot_sectors")
    if not isinstance(raw, list):
        return []
    candidates: list[dict[str, object]] = []
    seen_names: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        if item.get("cycle") == "none":
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            continue
        key = name.strip()
        if key in seen_names:
            continue
        seen_names.add(key)
        candidates.append(item)
        if len(candidates) >= _WIND_LEADERS_LIMIT:
            break
    return candidates


async def _load_cause_sector_codes(report_date: str) -> set[str]:
    """读当日 sector_trace 报告，聚合主因板块 resolve 后 ts_code 集合（排除用）。

    主因板块当日已由 review_done 级联预判（sector_trace 报告每板块一条，
    display_report.sectors=[板块中文名]），故用 list 接口读全部报告并逐名 resolve。
    报告不存在 / 结构不符 / 读取失败 / 单名 resolve 失败 → 视为无主因（不排除，
    宁放过不误杀——主因板块即使漏排，幂等检查仍会拦下已落库记录）。
    """
    cause_codes: set[str] = set()
    try:
        reports = await node_api.list_analysis_reports(_SECTOR_TRACE_REPORT_TYPE, report_date)
    except Exception as exc:  # noqa: BLE001 — 读取失败降级为"无主因"，不阻断主任务
        logger.warning(
            "sector_wind.cause_report_read_failed",
            report_date=report_date,
            error=str(exc),
        )
        return cause_codes
    for report in reports:
        if not isinstance(report, dict):
            continue
        content = report.get("content")
        display = content.get("display_report") if isinstance(content, dict) else None
        raw_sectors = display.get("sectors") if isinstance(display, dict) else None
        if not isinstance(raw_sectors, list):
            continue
        for sector_name in raw_sectors:
            if not isinstance(sector_name, str) or not sector_name.strip():
                continue
            resolved = await resolve_sector_target(sector_name)
            if resolved is None:
                continue
            ts_code = resolved.get("ts_code")
            if ts_code:
                cause_codes.add(ts_code)
    return cause_codes


def _make_stats() -> dict[str, int]:
    """任务统计骨架（键均为合法标识符，可直接 **展开为结构化日志字段）。"""
    return {
        "wind_sectors": 0,
        "resolve_failed": 0,
        "cause_excluded": 0,
        "idempotent_skipped": 0,
        "predicted": 0,
        "failed": 0,
    }


async def run_sector_wind_prediction(report_date: str | None = None) -> dict[str, int]:
    """执行一次风口板块批量预判（板块四环 spec §6.3，scheduler 交易日 19:30 调用）。

    Args:
        report_date: 报告交易日 YYYY-MM-DD；缺省取上海时区自然日
            （shanghai_today，与 prediction_validate 任务口径一致）。

    Returns:
        统计 dict：wind_sectors（候选板块数）/ resolve_failed（resolve 失败跳过）/
        cause_excluded（主因板块排除）/ idempotent_skipped（幂等命中跳过）/
        predicted（predict_sector 产出成功数）/ failed（产出失败数）。
        失败不抛异常（记日志），调度器入口再做最终兜底。
    """
    date_str = report_date or shanghai_today().isoformat()
    stats = _make_stats()
    try:
        data = await node_api.get(_WIND_LEADERS_PATH)
    except Exception as exc:  # noqa: BLE001 — 拉榜失败整体降级，不向调度器抛
        logger.warning(
            "sector_wind.wind_leaders_fetch_failed",
            report_date=date_str,
            error=str(exc),
        )
        return stats
    if not isinstance(data, dict):
        logger.warning("sector_wind.wind_leaders_unavailable", report_date=date_str)
        return stats
    candidates = _filter_wind_sectors(data)
    if not candidates:
        logger.info("sector_wind.empty", report_date=date_str)
        return stats
    stats["wind_sectors"] = len(candidates)
    cause_codes = await _load_cause_sector_codes(date_str)
    seen_codes: set[str] = set()
    for row in candidates:
        try:
            raw_name = row.get("name")
            sector_name = raw_name.strip() if isinstance(raw_name, str) else ""
            resolved = await resolve_sector_target(sector_name)
            if resolved is None:
                # 无法归一即无法与画像/聚合可靠 join，宁缺毋滥
                stats["resolve_failed"] += 1
                logger.info(
                    "sector_wind.resolve_failed",
                    report_date=date_str,
                    sector_name=sector_name or raw_name,
                )
                continue
            resolved_name = resolved.get("name") or sector_name
            ts_code = resolved.get("ts_code")
            if not ts_code:
                stats["resolve_failed"] += 1
                logger.info(
                    "sector_wind.resolve_no_code",
                    report_date=date_str,
                    sector_name=sector_name,
                )
                continue
            if ts_code in seen_codes:
                continue  # 别名（如 白酒/白酒概念）解析到同板块，靠前已处理
            seen_codes.add(ts_code)
            if ts_code in cause_codes:
                # 主因板块已由 review_done 级联预判落库，避免同日同板块重复扣费
                stats["cause_excluded"] += 1
                logger.info(
                    "sector_wind.cause_excluded",
                    report_date=date_str,
                    sector_name=resolved_name,
                )
                continue
            source_id = f"sector:{resolved_name}:{date_str}"
            try:
                existing = await node_api.list_predictions(source_id)
            except Exception as exc:  # noqa: BLE001
                # 幂等查询失败无法确认是否已有记录：fail-safe 跳过（宁可不产，
                # 不可裸覆盖可能已验证的记录——Node upsert 会重置 status=pending）
                stats["idempotent_skipped"] += 1
                logger.warning(
                    "sector_wind.idempotency_check_failed",
                    report_date=date_str,
                    sector_name=resolved_name,
                    source_id=source_id,
                    error=str(exc),
                )
                continue
            if existing:
                stats["idempotent_skipped"] += 1
                logger.info(
                    "sector_wind.idempotent_skip",
                    report_date=date_str,
                    sector_name=resolved_name,
                    source_id=source_id,
                )
                continue
            prediction = await predict_sector(
                report_date=date_str,
                sector_name=resolved_name,
                sector_snapshot=_build_sector_snapshot(row, resolved_name),
            )
            if prediction is None:
                # predict_sector 内部已记 warning（resolve/LLM/落库任一失败）
                stats["failed"] += 1
                logger.info(
                    "sector_wind.prediction_failed",
                    report_date=date_str,
                    sector_name=resolved_name,
                )
                continue
            stats["predicted"] += 1
            logger.info(
                "sector_wind.predicted",
                report_date=date_str,
                sector_name=resolved_name,
                source_id=source_id,
                prediction_status=prediction.prediction_status,
            )
        except Exception as exc:  # noqa: BLE001 — 单板块异常不拖垮整批
            stats["failed"] += 1
            logger.warning(
                "sector_wind.sector_failed",
                report_date=date_str,
                error=str(exc),
                exc_info=True,
            )
    logger.info("sector_wind.done", report_date=date_str, **stats)
    return stats
