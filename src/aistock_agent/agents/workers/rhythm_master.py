"""节奏大师 Worker（spec §8/D13）。

三时点统一走 `_compose_card` 三层证据流水线：
- after_close（target_date=下一交易日）：全量证据重算 + 合成 → 落盘；
- morning / midday（target_date=运行日）：读最新事件 result / kline 后重算证据，
  天然满足事件驱动增量语义；为控制成本，过度重生成的缓存优化列入开放点。
"""
from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from typing import Any, cast, get_args

from aistock_agent.config import settings
from aistock_agent.schemas.rhythm_master import MasterRhythmCard, RhythmEvidence, Stage
from aistock_agent.services import rhythm_engine as engine
from aistock_agent.services import rhythm_rebuilt_evidence as ev
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_calendar import EventWindow, load_event_window
from aistock_agent.services.rhythm_rebuilt_synthesis import run_synthesis
from aistock_agent.services.rhythm_rebuilt_validate import validate_synthesis
from aistock_agent.utils.date import add_trading_days, shanghai_today
from aistock_agent.utils.paths import project_root

logger = logging.getLogger(__name__)

REFRESH_SLOTS = ("after_close", "morning", "midday")

# sentiment 归档目录（settings 值 + 仓库根解析，不依赖 CWD；测试可覆写）
sentiment_archive_dir = project_root() / settings.sentiment_output_dir

INDEX_CODE = "000001"  # 上证指数

KLINE_LOOKBACK = 200  # 对齐 Node /internal/index/:code/kline 的 days 上限（1-200）；
# 传 end_date 时该参数仍须在限内（G2/G9 裁决）
MIN_KLINE_ROWS = 20   # 对齐 rhythm_rebuilt_evidence._trend_score/_volume_score 的
# len<20 短路下限（G2 裁决）

DEGRADED_TEXT = "节奏大师生成暂时不可用，请稍后重试"

DEGRADED_MODEL = "研研判暂不可用"


def _normalize_ymd(value: object) -> str | None:
    """把 trade_date 归一为 YYYYMMDD（容忍 YYYY-MM-DD / 空）。G3：比对前必须归一。"""
    if value is None:
        return None
    text = str(value).replace("-", "").strip()
    return text or None


def _event_confirm(events: list[dict[str, Any]]) -> bool:
    """事件确认（G3）：与 event_anchors/event_branches 同源，仅认 high 级事件。

    result ∈ {超预期, 不及预期} 才算「已确认方向」；medium/low 事件不得抬 certainty
    （否则出现「有确认、无锚点」的矛盾卡）。
    """
    return any(
        e.get("importance") == "high" and e.get("result") in {"超预期", "不及预期"}
        for e in events
    )


def _inherit_basis_stage(
    slot: str, basis_response: object
) -> tuple[str | None, str] | None:
    """morning/midday 主档位沿用最近 after_close 基准（AGENTS.md：「主档位沿用收盘基准结论」）。

    仅 after_close 之外的两个时点继承；无基准卡时返回 None（调用方留痕）；基准
    stage 非法/为空或 evidence 残缺时返回 None（调用方本地重算，不另留痕）。
    G2：消灭「同日日历格 ice / 详情页 low」的自相矛盾。

    真实契约：`NodeApiClient._request` 已解包 `code==200` 信封，`get_rhythm_report`
    返回的业务对象把 `content` 放在顶层（对齐 rhythm_verification.py 的 `resp.get("content")`）。
    """
    if slot not in {"morning", "midday"}:
        return None
    if not isinstance(basis_response, dict):
        return None
    content = basis_response.get("content")
    if not isinstance(content, dict):
        return None
    evidence = content.get("evidence")
    if not isinstance(evidence, dict):
        return None
    stage = evidence.get("stage")
    if not isinstance(stage, str) or not stage:
        return None
    # 越界 stage 必须在此拦截：该值会流入 RhythmEvidence.stage（Stage|None 的
    # Literal），一旦是 Node 侧回读的野值，将在构造时抛 ValidationError 打断整轮刷新
    if stage not in get_args(Stage):
        return None
    basis_date = content.get("basis_date")
    reason = str(evidence.get("stage_reason") or "")
    return stage, f"沿用收盘基准（{basis_date or '—'}）：{reason}"


def _amount_yi(raw: float | None) -> float:
    """Tushare index_daily 的 amount 单位是千元，engine/前端成交额分支按"亿元"计
    （1 亿 = 1e5 千元，常量见 rhythm_engine.QIAN_YUAN_TO_YI）。缺失/非法如实转 0.0
    （量能仅参与 ratio 与均量阈值，0 不伪造）。"""
    return (raw * engine.QIAN_YUAN_TO_YI) if raw is not None else 0.0


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


def _volume_confirm(amounts: list[float], stage: str | None) -> str | None:
    """按量能三档 + 阶段给量价确认方向（确定性，不靠 LLM）。"""
    if len(amounts) < 20 or stage is None:
        return None
    avg20 = sum(amounts[-20:]) / 20
    avg5 = sum(amounts[-5:]) / 5
    if avg20 <= 0:
        return None
    ratio = avg5 / avg20
    if ratio >= 1.1 and stage in {"launch", "rally"}:
        return "bullish"
    if ratio <= 0.8 and stage in {"overheat", "ebb"}:
        return "bearish"
    return None


async def _compose_card(
    basis_date: str, slot: str
) -> tuple[MasterRhythmCard, list[dict[str, object]], EventWindow]:
    """三时点证据流水线：返回 (MasterRhythmCard, rows, win) 三元组。

    rows 为 close 非空过滤后的 K 线行（供 _build_rhythm_card 复用，避免二次取数）；
    win 为当前窗口 EventWindow（事件分支/锚点来源）。
    """
    target_date = (
        add_trading_days(date_cls.fromisoformat(basis_date), 1).isoformat()
        if slot == "after_close"
        else basis_date
    )
    basis_inherit_note: str | None = None
    basis_ymd = date_cls.fromisoformat(basis_date).strftime("%Y%m%d")
    kline = (
        await node_api.get_index_kline(INDEX_CODE, days=KLINE_LOOKBACK, end_date=basis_ymd) or []
    )
    rows = [r for r in kline if r.get("close") is not None]
    last_trade_date = _normalize_ymd(rows[-1].get("trade_date")) if rows else None
    # P0-2/G4 分槽门禁：after_close 的 basis 必须是"当日 K 线到位"的交易日；
    # morning/midday 的 basis 是运行日（盘中当日 bar 天然未出），不设该门禁。
    basis_gate = slot == "after_close" and (
        last_trade_date is None or last_trade_date != basis_ymd
    )
    kline_short = len(rows) < MIN_KLINE_ROWS
    if kline_short:
        logger.warning("rhythm_master.kline_insufficient n=%s basis=%s", len(rows), basis_date)
    closes = [float(r["close"]) for r in rows[-65:]]
    # Tushare index_daily amount 千元 → 亿元（engine 单位契约；2026-09-05 核实修复：
    # Node /internal/index/:code/kline 此前丢弃 vol/amount，恒 null → 量能伪分支）
    amounts = [
        _amount_yi(float(r["amount"]) if r.get("amount") is not None else None)
        for r in rows[-120:]
    ]
    fg = (await node_api.get_fear_greed() or {}).get("index")
    win = await load_event_window(target_date)
    _, sentiment_scores, _, _ = _load_sentiment_series(days=7)

    breadth = None
    snapshot_missing = False
    # G1：宽度证据必须与 K 线证据日同源（此前 get_last_close_snapshot() 取
    # 「严格早于今天」的最近交易日 → after_close(周五) 实际取周四宽度）。
    if last_trade_date is None:
        snapshot_missing = True
    else:
        snap = await node_api.get_close_snapshot(last_trade_date)
        if isinstance(snap, dict):
            breadth = snap.get("breadth")
        else:
            snapshot_missing = True

    if kline_short:
        stage: Stage | None = None
        stage_reason = "指数K线不足20根，趋势/量能判定不可用"
    elif basis_gate:
        stage = None
        stage_reason = "基准日无当日K线，趋势/量能判定不适用"
    else:
        stage, stage_reason = ev.detect_stage(
            breadth=breadth, closes=closes, amounts=amounts,
            sentiment_scores=sentiment_scores,
            fg=fg if isinstance(fg, int | float) else None,
            prev_phase=None,
        )
    # P0-2/G2：morning/midday 主档位沿用 after_close 基准，消除同日双档矛盾
    if slot in {"morning", "midday"} and stage is not None:
        basis_resp = await node_api.get_rhythm_report(target_date, "after_close")
        inherited = _inherit_basis_stage(slot, basis_resp)
        if inherited is not None:
            stage, stage_reason = cast("tuple[Stage | None, str]", inherited)
        elif basis_resp is None:
            basis_inherit_note = "收盘基准卡缺失（主档位未沿用）"
    event_confirm = _event_confirm(win.events)
    volume_direction = _volume_confirm(amounts, stage)
    cert, cert_reason = ev.detect_certainty(
        event_confirm=event_confirm, volume_direction=volume_direction,
        stage=stage, breadth=breadth,
    )
    position = ev.compute_position(stage=stage, certainty=cert)
    anchors = ev.build_event_anchors(win.events)

    missing: list[str] = []
    if kline_short:
        missing.append("指数K线不足")
    if basis_gate:
        missing.append("基准日无当日K线（非交易日或数据未就绪）")
    if snapshot_missing:
        missing.append("宽度快照缺失（证据日无收盘快照）")
    if basis_inherit_note:
        missing.append(basis_inherit_note)
    evidence = RhythmEvidence(
        stage=stage, stage_reason=stage_reason, certainty=cert, certainty_reason=cert_reason,
        position=position, event_anchors=anchors, data_missing=missing,
    )
    synthesis = await run_synthesis(evidence)
    synthesis_ok = synthesis is not None and validate_synthesis(synthesis, evidence)
    return (
        MasterRhythmCard(
            basis_date=basis_date, target_date=target_date, refresh_slot=slot,
            evidence=evidence, synthesis=synthesis if synthesis_ok else None,
            synthesis_available=synthesis_ok,
        ),
        rows,
        win,
    )


def _build_rhythm_card(
    card: MasterRhythmCard, win: EventWindow, rows: list[dict[str, object]]
) -> dict[str, object]:
    """按前端 RhythmCard 契约构造 rhythm_card（2026-09-05 裁决）。

    - score 由 level 派生同源（score=level_idx×20，见 STAGE_TO_LEVEL）；
    - branches 由 rhythm_engine 确定性生成（technical + event），不靠 LLM；
    - 可选字段缺失由前端 v-if 兜底（next_event_anchor/event_high_hint 等）。
    """
    from aistock_agent.schemas.rhythm_master import STAGE_TO_LEVEL  # F3 常量，score 派生同源

    level_entry = STAGE_TO_LEVEL.get(card.evidence.stage)
    level = level_entry["level"] if level_entry else None
    score = level_entry["score"] if level_entry else None
    closes = [float(r["close"]) for r in rows[-65:] if r.get("close") is not None]
    amounts = [
        _amount_yi(float(r["amount"]) if r.get("amount") is not None else None)
        for r in rows[-120:]
    ]
    highs = [float(r["high"]) if r.get("high") is not None else None for r in rows[-120:]]
    lows = [float(r["low"]) if r.get("low") is not None else None for r in rows[-120:]]
    missing = list(card.evidence.data_missing)
    data_missing_container: list[str] = list(card.evidence.data_missing)
    try:
        branches = engine.build_technical_branches(
            closes=closes, highs=highs, lows=lows, amounts=amounts,
            data_missing=data_missing_container,
        )
        for e in win.events:
            branches.extend(engine.build_event_branch(e))
    except Exception:
        logger.warning("rhythm_master.rhythm_card_branches_failed", exc_info=True)
        branches = []
    missing.extend(m for m in data_missing_container if m not in missing)
    return {
        "score": score,
        "level": level,
        "position_band": {
            "text": card.evidence.position.text if card.evidence.position else "",
        },
        "phase_evidence": {"reason": card.evidence.stage_reason, "slope": None},
        "basis_data_date": _normalize_ymd(rows[-1].get("trade_date")) if rows else None,
        "temperature_series": [],
        "event_window": [],
        "event_source_missing": win.source_missing,
        "next_event_anchor": engine.build_next_event_anchor(win.events, card.basis_date),
        # 暂无冲突检测器（Phase 4 态 ↔ Stage 5 态不同源，见 spec §2.2）：恒 False。
        # 前端 conflict 为必填 bool，不可置 null；接入检测器前保持此常量。
        "conflict": False,
        "branches": branches,
        "data_missing": missing,
    }


async def run(state: dict[str, object]) -> dict[str, object]:
    try:
        slot = str(state.get("refresh_slot") or "after_close")
        if slot not in REFRESH_SLOTS:
            slot = "after_close"
        basis = str(state.get("report_date") or shanghai_today().isoformat())
        card, rows, win = await _compose_card(basis, slot)
        if not card.synthesis_available:
            logger.warning(
                "rhythm_master.degraded reason=%s slot=%s target_date=%s",
                DEGRADED_MODEL, card.refresh_slot, card.target_date,
            )
        content = {
            "schema_version": "1.0",
            "target_date": card.target_date,
            "basis_date": card.basis_date,
            "refresh_slot": card.refresh_slot,
            "evidence": card.evidence.model_dump(),
            "synthesis": card.synthesis.model_dump() if card.synthesis else None,
            "synthesis_available": card.synthesis_available,
            "rhythm_card": _build_rhythm_card(card, win, rows),
        }
        await node_api.save_analysis_report(
            report_type="rhythm_master", report_date=card.target_date,
            user_id=slot, content=content, data_source="rhythm_master_agent",
            update_cache=False,
        )
        return {"final_response": json.dumps(content, ensure_ascii=False),
                "analysis_reports": {"rhythm_master": content}}
    except Exception:
        logger.exception("rhythm_master.run_failed")
        return {"final_response": DEGRADED_TEXT}
