"""节奏大师 Worker（spec §8/D13）。

三时点：
- after_close（16:05，target_date=下一交易日）：全量合成主档位 + 分支 + 提示 → 落盘；
- morning（9:00）/ midday（12:30）（target_date=当天）：只做事件驱动增量
  （预期差落档/分支触发/提示更新），主档位沿用 16:05 基准值，禁止重合成、禁止伪造当日温度（G18）。
"""
from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from pathlib import Path
from typing import Any

from aistock_agent.prompts.workers.rhythm_master import (
    RHYTHM_NARRATIVE_FALLBACK,
    build_narrative_prompt,
)
from aistock_agent.services import rhythm_engine
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_calendar import load_event_window
from aistock_agent.services.llm import get_quick_think
from aistock_agent.utils.date import add_trading_days, shanghai_today

logger = logging.getLogger(__name__)

REFRESH_SLOTS = ("after_close", "morning", "midday")

# sentiment 归档目录（对齐 config.sentiment_output_dir 默认值；测试可覆写）
sentiment_archive_dir = Path("docs/agent-outputs/sentiment")

INDEX_CODE = "000001"  # 上证指数

DEGRADED_TEXT = "节奏大师生成暂时不可用，请稍后重试"


def _event_high_hint(high_events: list[dict[str, Any]]) -> str:
    """high 事件前置提示文案（§7.1 事件前置纪律，I1）。

    after_close 基准卡与 morning/midday 增量共用同一文案逻辑；
    无 high 事件时返回空串（前端 v-if 不渲染）。
    """
    titles = [str(e.get("title", "")) for e in high_events]
    if not titles:
        return ""
    return (
        f"未来 {len(titles)} 日有 {'、'.join(titles[:2])} 等高影响事件，"
        "注意确定性风险，倾向相应收敛"
    )


def _load_sentiment_series(
    days: int = 7,
) -> tuple[list[dict[str, Any]], list[float], int, str | None]:
    """读 sentiment 归档近 N 个交易日序列。返回 (series, scores, consecutive_ice, latest_phase)。"""
    if not sentiment_archive_dir.exists():
        return [], [], 0, None
    files = sorted(sentiment_archive_dir.glob("*.json"))
    # latest.json 排除（避免与日期文件重复）
    files = [f for f in files if f.name != "latest.json"][-days:]
    series: list[dict[str, Any]] = []
    scores: list[float] = []
    consecutive_ice = 0
    latest_phase: str | None = None
    for f in files:
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        score = payload.get("score")
        if isinstance(score, int | float):
            series.append({"date": payload.get("date", f.stem), "score": float(score)})
            scores.append(float(score))
        ice = payload.get("ice") or {}
        if f == files[-1]:
            consecutive_ice = int(ice.get("consecutive_ice_days", 0) or 0)
            # cycle_phase 四态收窄（对齐 sentiment_temp 同款收窄，P7 加固）：脏值不入参 detect_phase
            raw_phase = payload.get("cycle_phase")
            latest_phase = (
                raw_phase
                if isinstance(raw_phase, str)
                and raw_phase in {"ice", "warm_up", "overheat", "ebb"}
                else None
            )
    return series, scores, consecutive_ice, latest_phase


async def _compose_after_close(basis_date: str) -> dict[str, Any] | None:
    """16:05 全量合成（读前校验 basis_date 温度已落盘，G18）。"""
    target_date = add_trading_days(date_cls.fromisoformat(basis_date), 1).isoformat()
    series, scores, consecutive_ice, latest_phase = _load_sentiment_series(days=7)
    missing: list[str] = []
    today_archive = sentiment_archive_dir / f"{basis_date}.json"
    if not today_archive.exists():
        missing.append("情绪数据缺失（沿用前值）")
    prev_phase = latest_phase
    kline = await node_api.get_index_kline(INDEX_CODE, days=120) or []  # 60→120：MA60 佐证需 ≥65 根
    closes = [float(r["close"]) for r in kline if r.get("close") is not None]
    highs = [float(r["high"]) for r in kline if r.get("high") is not None]
    lows = [float(r["low"]) for r in kline if r.get("low") is not None]
    amounts = [float(r["amount"]) for r in kline if r.get("amount") is not None]
    # C1：指数技术位多级确认佐证（确定性计算，LLM 不产数值）；不足 65 根时如实标注
    breadth = (
        rhythm_engine.ma_breadth(closes)
        if len(closes) >= 65
        else rhythm_engine.ma_breadth([])
    )
    if breadth.get("insufficient"):
        missing.append("MA 技术位数据不足")
    trend = rhythm_engine.trend_anchor(closes, amounts)
    fg_resp = await node_api.get_fear_greed()
    fg_index = fg_resp.get("index") if isinstance(fg_resp, dict) else None
    # 阶段：以 payload.cycle_phase 为 prev 输入、量能真实证据重算（单源真相 = engine）
    volume_weak = None
    if amounts and len(amounts) >= 20:
        volume_weak = sum(amounts[-5:]) / 5 < sum(amounts[-20:]) / 20 * 0.8
    # C1 前原始 phase 判背离（同一价格信号只经一条路径生效：tech 佐证不进 detect_conflict 输入）
    base_phase, _ = rhythm_engine.detect_phase(
        history=scores,
        consecutive_ice=consecutive_ice,
        volume_weak=volume_weak,
        prev_phase=prev_phase,
        tech=None,
    )
    conflict, conflict_detail = rhythm_engine.detect_conflict(base_phase, trend)
    penalty = rhythm_engine.conflict_penalty(rhythm_engine.conflict_kind(base_phase, trend))

    # 展示相位（tech 佐证只影响展示与 evidence，不进入背离判定）
    phase, phase_evidence = rhythm_engine.detect_phase(
        history=scores,
        consecutive_ice=consecutive_ice,
        volume_weak=volume_weak,
        prev_phase=prev_phase,
        tech=breadth,  # C1：技术佐证只进 phase_evidence，不进仓位话术
    )
    phase_evidence["technical"] = breadth
    score, compose_missing = rhythm_engine.compose_score(
        phase=phase,
        trend=trend,
        fg=fg_index,
        trend_available=trend is not None,
        fg_available=fg_index is not None,
        penalty=penalty,  # C2：顶背离降 1 档；底背离 0.0 禁止降档
    )
    missing.extend(compose_missing)
    level = rhythm_engine.level_from_score(score)
    win = await load_event_window(target_date)
    branches: list[dict[str, Any]] = []
    if win.high_events:
        branches = rhythm_engine.build_technical_branches(
            closes=closes, highs=highs, lows=lows, amounts=amounts
        )[:2]
        ev = rhythm_engine.build_event_branch(win.high_events[0])
        if ev is not None:
            branches.append(ev)
    else:
        branches = rhythm_engine.build_technical_branches(
            closes=closes, highs=highs, lows=lows, amounts=amounts
        )[:3]
    if win.source_missing:
        missing.append("事件源未接（日历接口不可用）")
    if win.calendar_uncovered:
        missing.append("交易日历未覆盖（事件窗口不可用）")
    card = {
        "score": round(score, 1),
        "level": level,
        "position_band": rhythm_engine.position_band(level),
        "phase": phase,
        "phase_evidence": {**phase_evidence, "experimental": True},
        "temperature_series": series,  # 复用本函数已加载的 series，避免二次读盘
        "event_window": win.events,
        "event_source_missing": win.source_missing,
        "event_high_hint": _event_high_hint(win.high_events),
        "conflict": conflict,
        "conflict_detail": conflict_detail,
        "branches": branches,
        "data_missing": missing,
    }
    return {
        "target_date": target_date,
        "basis_date": basis_date,
        "refresh_slot": "after_close",
        "rhythm_card": card,
    }


async def _apply_event_delta(
    base: dict[str, Any], slot: str, basis_date: str
) -> dict[str, Any]:
    """morning/midday 事件驱动增量：主档位沿用基准，只更新事件窗口/分支触发/提示（G18）。

    basis_date 语义随刷新推进（spec §8：16:05=当日收盘基准 / 9:00=当日盘前隔夜 /
    12:30=当日午间）；增量版本 basis_date=运行日 为规格语义，主档位数据基准日由
    基准报告（after_close 版）自含，故此处不改 basis 语义、不重合成主档位。
    """
    card = dict(base.get("rhythm_card", {}))
    # data_missing 从基准卡拷贝后独立维护（不改写基准报告；保持缺失标注如实）
    missing = list(card.get("data_missing") or [])
    win = await load_event_window(basis_date)
    card["event_window"] = win.events
    card["event_source_missing"] = win.source_missing
    # 事件窗口缺失标注：calendar_uncovered / source_missing 按当前窗口状态追加/移除（对齐全量分支）
    if win.calendar_uncovered:
        if "交易日历未覆盖（事件窗口不可用）" not in missing:
            missing.append("交易日历未覆盖（事件窗口不可用）")
    else:
        missing = [m for m in missing if m != "交易日历未覆盖（事件窗口不可用）"]
    if win.source_missing:
        if "事件源未接（日历接口不可用）" not in missing:
            missing.append("事件源未接（日历接口不可用）")
    else:
        missing = [m for m in missing if m != "事件源未接（日历接口不可用）"]
    card["data_missing"] = missing
    # 事件分支落档：公布后按预期差触发（§19.3/D11）；v1 以 result 字段人工回填为主
    new_branches: list[dict[str, Any]] = []
    for br in card.get("branches", []):
        ref = br.get("event_ref")
        if ref:
            matched = [
                e
                for e in win.events
                if str(e.get("date", "")) == str(ref.get("event_date", ""))
                and str(e.get("title", "")) == str(ref.get("title", ""))
            ]
            result = matched[0].get("result") if matched else None
            if result in {"超预期", "符合", "不及预期"}:
                br["condition"]["value"] = result
                # D11：目标区间由 engine 确定性给（技术分支 range 来自 engine，G19），
                # 按预期差方向取对应技术分支区间填入，LLM 不参与点位；找不到保持 ""（保守 miss）。
                direction = {"超预期": "bullish", "符合": "neutral", "不及预期": "bearish"}[result]
                event_range = ""
                for tb in card.get("branches", []):
                    tcond = tb.get("condition") or {}
                    tconcl = tb.get("conclusion") or {}
                    if tcond.get("kind") == "interval" and tconcl.get("direction") == direction:
                        event_range = str(tconcl.get("range", "") or "")
                        break
                br["conclusion"]["range"] = event_range
                br["conclusion"]["note"] = (
                    f"事件结果已公布：{result}，按预期差落档，"
                    "目标区间由 engine 按当日行情计算"
                )
            else:
                br["conclusion"]["note"] = "结果待公布，公布后按预期差落档"
        new_branches.append(br)
    card["branches"] = new_branches
    # 提示层：high 事件前置提示（不改主档位，§7.1 事件前置纪律；与 after_close 基准卡共用文案，I1）
    card["event_high_hint"] = _event_high_hint(win.high_events)
    return {**base, "basis_date": basis_date, "refresh_slot": slot, "rhythm_card": card}


async def _narrate(card: dict[str, Any]) -> dict[str, Any]:
    """LLM 叙事（quick_think，禁点位）；失败降级模板（§7.2）。"""
    try:
        resp = await get_quick_think().ainvoke(
            [{"role": "user", "content": build_narrative_prompt(card)}]
        )
        text = getattr(resp, "content", None) or ""
        parsed = json.loads(text) if isinstance(text, str) else {}
        if not isinstance(parsed, dict) or not parsed.get("summary"):
            return dict(RHYTHM_NARRATIVE_FALLBACK)
        risks = parsed.get("risks")
        if not isinstance(risks, list) or not risks:
            risks = [rhythm_engine.DISCLAIMER]
        return {
            "summary": str(parsed["summary"])[:50],
            "details": str(parsed.get("details", "")),
            "risks": [str(r) for r in risks],
        }
    except Exception:
        logger.warning("rhythm_master.narrative_fallback")
        return dict(RHYTHM_NARRATIVE_FALLBACK)


async def run(state: dict[str, object]) -> dict[str, object]:
    """Worker 入口（顶层 try-catch 降级，不抛异常，G6/§10）。"""
    try:
        slot = str(state.get("refresh_slot") or "after_close")
        if slot not in REFRESH_SLOTS:
            slot = "after_close"
        basis_date = str(state.get("report_date") or shanghai_today().isoformat())
        if slot == "after_close":
            payload = await _compose_after_close(basis_date)
            if payload is None:
                return {"final_response": DEGRADED_TEXT}
        else:
            # 基准 = 上一交易日 16:05 生成的 target_date=当天的 after_close 版本（D13）
            target_date = basis_date
            base_resp = await node_api.get_rhythm_report(target_date, "after_close")
            base = None
            if isinstance(base_resp, dict):
                base = base_resp.get("content")
            if not isinstance(base, dict) or "rhythm_card" not in base:
                card_fallback = {
                    "score": None,
                    "level": None,
                    "position_band": {"text": "沿用前值"},
                    "branches": [],
                    "data_missing": ["基准报告缺失（沿用前值）"],
                }
                payload = {
                    "target_date": target_date,
                    "basis_date": basis_date,
                    "refresh_slot": slot,
                    "rhythm_card": card_fallback,
                }
            else:
                payload = await _apply_event_delta(base, slot, basis_date)
        card = payload["rhythm_card"]
        narrative = await _narrate(card)
        content = {
            "display_report": narrative,
            "schema_version": "2.0",
            "target_date": payload["target_date"],
            "basis_date": payload["basis_date"],
            "refresh_slot": payload["refresh_slot"],
            "rhythm_card": card,
        }
        # 三版本独立落盘：user_id 列承载 refresh_slot（event_conduction 隔离先例，契约 #6）
        await node_api.save_analysis_report(
            report_type="rhythm_master",
            report_date=payload["target_date"],
            user_id=slot,
            content=content,
            data_source="rhythm_master_agent",
            update_cache=False,
        )
        return {
            "final_response": json.dumps(content, ensure_ascii=False),
            "analysis_reports": {"rhythm_master": content},
        }
    except Exception:
        logger.exception("rhythm_master.run_failed")
        return {"final_response": DEGRADED_TEXT}
