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
from pathlib import Path
from typing import Any

from aistock_agent.schemas.rhythm_master import MasterRhythmCard, RhythmEvidence, Stage
from aistock_agent.services import rhythm_rebuilt_evidence as ev
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_calendar import load_event_window
from aistock_agent.services.rhythm_rebuilt_synthesis import run_synthesis
from aistock_agent.services.rhythm_rebuilt_validate import validate_synthesis
from aistock_agent.utils.date import add_trading_days, shanghai_today

logger = logging.getLogger(__name__)

REFRESH_SLOTS = ("after_close", "morning", "midday")

# sentiment 归档目录（对齐 config.sentiment_output_dir 默认值；测试可覆写）
sentiment_archive_dir = Path("docs/agent-outputs/sentiment")

INDEX_CODE = "000001"  # 上证指数

KLINE_LOOKBACK = 200  # 对齐 Node /internal/index/:code/kline 的 days 上限（1-200）；
# 传 end_date 时该参数仍须在限内（G2/G9 裁决）
MIN_KLINE_ROWS = 20   # 对齐 rhythm_rebuilt_evidence._trend_score/_volume_score 的
# len<20 短路下限（G2 裁决）

DEGRADED_TEXT = "节奏大师生成暂时不可用，请稍后重试"

DEGRADED_MODEL = "研研判暂不可用"


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


async def _compose_card(basis_date: str, slot: str) -> MasterRhythmCard | None:
    target_date = (
        add_trading_days(date_cls.fromisoformat(basis_date), 1).isoformat()
        if slot == "after_close"
        else basis_date
    )
    basis_ymd = date_cls.fromisoformat(basis_date).strftime("%Y%m%d")
    kline = (
        await node_api.get_index_kline(INDEX_CODE, days=KLINE_LOOKBACK, end_date=basis_ymd) or []
    )
    rows = [r for r in kline if r.get("close") is not None]
    kline_short = len(rows) < MIN_KLINE_ROWS
    if kline_short:
        logger.warning("rhythm_master.kline_insufficient n=%s basis=%s", len(rows), basis_date)
    closes = [float(r["close"]) for r in rows[-65:]]
    amounts = [float(r["amount"]) if r.get("amount") is not None else 0.0 for r in rows[-120:]]
    fg = (await node_api.get_fear_greed() or {}).get("index")
    win = await load_event_window(target_date)
    _, sentiment_scores, _, _ = _load_sentiment_series(days=7)

    breadth = None
    snap = await node_api.get_last_close_snapshot()
    if isinstance(snap, dict):
        breadth = snap.get("breadth")

    if kline_short:
        stage: Stage | None = None
        stage_reason = "指数K线不足20根，趋势/量能判定不可用"
    else:
        stage, stage_reason = ev.detect_stage(
            breadth=breadth, closes=closes, amounts=amounts,
            sentiment_scores=sentiment_scores,
            fg=fg if isinstance(fg, int | float) else None,
            prev_phase=None,
        )
    event_confirm = any(e.get("result") in {"超预期", "不及预期"} for e in win.events)
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
    evidence = RhythmEvidence(
        stage=stage, stage_reason=stage_reason, certainty=cert, certainty_reason=cert_reason,
        position=position, event_anchors=anchors, data_missing=missing,
    )
    synthesis = await run_synthesis(evidence)
    synthesis_ok = synthesis is not None and validate_synthesis(synthesis, evidence)
    return MasterRhythmCard(
        basis_date=basis_date, target_date=target_date, refresh_slot=slot,
        evidence=evidence, synthesis=synthesis if synthesis_ok else None,
        synthesis_available=synthesis_ok,
    )


async def run(state: dict[str, object]) -> dict[str, object]:
    try:
        slot = str(state.get("refresh_slot") or "after_close")
        if slot not in REFRESH_SLOTS:
            slot = "after_close"
        basis = str(state.get("report_date") or shanghai_today().isoformat())
        card = await _compose_card(basis, slot)
        if card is None:
            return {"final_response": DEGRADED_TEXT}
        if not card.synthesis_available:
            card.evidence.data_missing.append(DEGRADED_MODEL)
        content = {
            "schema_version": "1.0",
            "target_date": card.target_date,
            "basis_date": card.basis_date,
            "refresh_slot": card.refresh_slot,
            "evidence": card.evidence.model_dump(),
            "synthesis": card.synthesis.model_dump() if card.synthesis else None,
            "synthesis_available": card.synthesis_available,
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
