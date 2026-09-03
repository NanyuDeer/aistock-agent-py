"""分支验证（spec §19.4，硬约束 14）。

- 窗口：target_date 起 5 个交易日（与 horizon 短期语义对齐）。
- 判定：站稳=连续 2 日收盘进入 conclusion.range → hit；条件触发但未站稳 → miss；
  条件未触发（partition 未出现）→ insufficient。
- 事件分支（D11）：partition 触发 = 事件 result 公布后按预期差落档（§19.3 事件结果通道），
  落档后按窗口 range 判 hit/miss，不再恒 insufficient。
- 分支命中独立统计，不并入既有 prediction hitRate（D6）。
"""
from __future__ import annotations

import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Literal

from aistock_agent.services.data_client import node_api
from aistock_agent.utils.date import add_trading_days, shanghai_today

logger = logging.getLogger(__name__)

Result = Literal["hit", "miss", "insufficient"]

# 验证统计归档
verification_dir = Path("docs/agent-outputs/rhythm")

WINDOW_DAYS = 5


def _parse_range(range_text: str) -> tuple[float, float] | None:
    try:
        lo_s, hi_s = str(range_text).split("-", 1)
        return float(lo_s), float(hi_s)
    except (ValueError, AttributeError):
        return None


def _triggered(
    cond: dict[str, Any],
    rows: list[dict[str, Any]],
    event_results: dict[str, str],
    event_title: str | None = None,
) -> bool:
    kind = cond.get("kind")
    indicator = str(cond.get("indicator", ""))
    if kind == "enum":
        # D11：优先用 event_ref.title 精确匹配事件 result（title 可能含"预期差"字样，
        # indicator 去前缀会误匹配），indicator 去"预期差"仅作无 event_ref 时的 fallback。
        title = event_title or indicator.replace("预期差", "")
        result = event_results.get(title)
        return result == cond.get("value")
    lo = cond.get("lo")
    hi = cond.get("hi")
    for row in rows:
        if indicator == "成交额":
            val = row.get("amount")
        else:
            val = row.get("close")
        if val is None:
            continue
        if lo is not None and float(val) < float(lo):
            continue
        if hi is not None and float(val) > float(hi):
            continue
        return True
    return False


def evaluate_branch(
    branch: dict[str, Any], rows: list[dict[str, Any]], event_results: dict[str, str]
) -> Result:
    """单分支判定。rows 为窗口（升序）指数日 K。"""
    cond = branch.get("condition") or {}
    conclusion = branch.get("conclusion") or {}
    ref = branch.get("event_ref")
    event_title = str(ref["title"]) if isinstance(ref, dict) and ref.get("title") else None
    if not _triggered(cond, rows, event_results, event_title):
        return "insufficient"
    # 有 anchor：以 direction + 触发位机械判 hit/miss（Task3）。
    # bullish → 站上触发位（cond.lo），bearish → 跌破触发位（cond.hi），
    # neutral → 沿 conclusion.range；均要求窗口内连续 2 日成立才算 hit（站稳）。
    anchor = branch.get("anchor")
    if isinstance(anchor, dict):
        direction = anchor.get("direction")
        trigger_val: float | None = None
        if cond.get("kind") == "interval":
            if direction == "bullish":
                lo = cond.get("lo")
                trigger_val = float(lo) if lo is not None else None
            elif direction == "bearish":
                hi = cond.get("hi")
                trigger_val = float(hi) if hi is not None else None
        # 机械判定前提：bullish/bearish 需能定位触发位；enum（事件）分支无数值触发位，
        # 不在此判定，落回下方 range 回退（D11：事件落档后按 range 判 hit/miss）。
        can_judge = (
            (direction in ("bullish", "bearish") and trigger_val is not None)
            or direction == "neutral"
        )
        if can_judge:
            consecutive = 0
            for row in rows:
                close = row.get("close")
                if close is None:
                    continue
                ok = False
                if direction == "bullish" and trigger_val is not None:
                    ok = float(close) > trigger_val
                elif direction == "bearish" and trigger_val is not None:
                    ok = float(close) < trigger_val
                elif direction == "neutral":
                    parsed_range = _parse_range(str(conclusion.get("range", "")))
                    if parsed_range:
                        lo, hi = parsed_range
                        ok = lo <= float(close) <= hi
                if ok:
                    consecutive += 1
                    if consecutive >= 2:
                        return "hit"
                else:
                    consecutive = 0
            return "miss"
    # 无 anchor（或 anchor 无法机械判定）：回退旧 range 判定（兼容）
    parsed = _parse_range(str(conclusion.get("range", "")))
    if parsed is None:
        return "miss"
    lo, hi = parsed
    consecutive = 0
    for row in rows:
        close = row.get("close")
        if close is None:
            continue
        if lo <= float(close) <= hi:
            consecutive += 1
            if consecutive >= 2:
                return "hit"
        else:
            consecutive = 0
    return "miss"


def hit_rate_summary(results: list[Result]) -> dict[str, Any]:
    hit = results.count("hit")
    miss = results.count("miss")
    insufficient = results.count("insufficient")
    judged = hit + miss
    return {
        "hit": hit,
        "miss": miss,
        "insufficient": insufficient,
        "total": len(results),
        "hit_rate": hit / judged if judged else None,
    }


async def run_once(report_date: str | None = None) -> dict[str, Any]:
    """扫描已满窗口的 rhythm_master 报告，按 §19.4 判各分支，统计独立指标
    （不并入 prediction hitRate）。

    数据源：`/internal/analysis-reports/rhythm_master/{target}/{slot}`
    + 窗口指数 K 线 + 事件 result。
    事件在 morning/midday 增量版本落档（after_close 版为占位，D11），故依次读
    最新 refresh_slot 版本（midday → morning → after_close），取第一个非空。
    v1 以手动/测试驱动为主（scheduler job 由 rhythm_verification_enabled 控制，默认关）。
    """
    target = report_date or shanghai_today().isoformat()
    try:
        end = add_trading_days(date.fromisoformat(target), WINDOW_DAYS)
    except ValueError:
        logger.warning("rhythm_verification.calendar_out_of_range target=%s", target)
        return {"report_date": target, "error": "交易日历未覆盖"}
    # 顶层 try：IO/网络异常不抛（对齐 rhythm_master.run 先例，G6/§10）
    try:
        rows_raw = await node_api.get_index_kline(
            "000001",
            days=200,
            start_date=target.replace("-", ""),
            end_date=end.isoformat().replace("-", ""),
        )
        rows = list(rows_raw) if isinstance(rows_raw, list) else []
        if not rows:
            return {"report_date": target, "evaluated": 0, "error": "窗口 K 线不可用"}
        # 事件落档只发生在 morning/midday 版本，after_close 为占位（D11）
        base = None
        for slot in ("midday", "morning", "after_close"):
            resp = await node_api.get_rhythm_report(target, slot)
            content = resp.get("content") if isinstance(resp, dict) else None
            if isinstance(content, dict) and "rhythm_card" in content:
                base = resp
                break
        if not isinstance(base, dict):
            return {"report_date": target, "evaluated": 0, "error": "基准报告缺失"}
        content = base.get("content")
        if not isinstance(content, dict):
            return {"report_date": target, "evaluated": 0, "error": "基准报告内容缺失"}
        card_raw = content.get("rhythm_card")
        card = card_raw if isinstance(card_raw, dict) else {}
        branches_raw = card.get("branches")
        branches = branches_raw if isinstance(branches_raw, list) else []
        events_raw = await node_api.get_calendar_events(target, end.isoformat())
        event_results: dict[str, str] = {}
        if isinstance(events_raw, list):
            for e in events_raw:
                if not e.get("title"):
                    continue
                result = e.get("result")
                if isinstance(result, str):
                    event_results[str(e["title"])] = result
        results = [evaluate_branch(b, rows, event_results) for b in branches]
        summary = hit_rate_summary(results)
        verification_dir.mkdir(parents=True, exist_ok=True)
        out = {
            "report_date": target,
            "evaluated": len(results),
            "results": results,
            "summary": summary,
        }
        (verification_dir / "verification.json").write_text(
            json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return out
    except Exception:
        logger.exception("rhythm_verification.run_once_failed target=%s", target)
        return {"report_date": target, "evaluated": 0, "error": "验证执行异常"}
