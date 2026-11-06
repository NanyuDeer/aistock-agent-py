"""节奏大师 Worker（spec §8/D13）。

三时点统一走 `_compose_card` 三层证据流水线：
- after_close（target_date=下一交易日）：全量证据重算 + 合成 → **定档**（唯一有权确立档位）；
- morning（target_date=运行日）：证据重算后**串行继承** after_close 的档位，只允许
  事件维度 ±1 修正（职责重定义 spec §4.1/§4.2）；
- midday（target_date=运行日）：证据重算后串行继承 **morning** 的档位（morning 不可用
  才回退 after_close），再用当日盘中数据做**单向降档**（spec §4.3），并把盘中因子
  恒落库到 `phase_evidence.intraday_factors`（spec §4.4，阈值可回测的前提）。

继承链是**串联**的：after_close → morning → midday。为控制成本，过度重生成的缓存
优化列入开放点。
"""
from __future__ import annotations

import json
import logging
from datetime import date as date_cls
from datetime import timedelta
from typing import Any, cast, get_args

from pydantic import ValidationError

from aistock_agent.config import settings
from aistock_agent.schemas.rhythm_master import (
    CandidateOutlook,
    DeltaReview,
    LedgerItem,
    MasterRhythmCard,
    PositionBand,
    PositionLedger,
    RhythmEvidence,
    Stage,
    StageSource,
)
from aistock_agent.services import intraday_snapshot_archive as intraday_archive
from aistock_agent.services import rhythm_delta as delta_svc
from aistock_agent.services import rhythm_delta_narrative as delta_narrative
from aistock_agent.services import rhythm_engine as engine
from aistock_agent.services import rhythm_rebuilt_evidence as ev
from aistock_agent.services.data_client import node_api
from aistock_agent.services.event_calendar import (
    EventWindow,
    split_analysis_window,
)
from aistock_agent.services.event_timeline import load_event_timeline
from aistock_agent.services.mainline_engine import (
    MA20_MIN_BARS,
    MIN_CANDIDATES,
    RET_WINDOW,
    build_mainline_notes,
    candidate_name_matches,
    detect_breakdown,
    judge_mainline,
    load_mainline_candidates,
    nav_from_pct,
    rank_candidates,
)
from aistock_agent.services.rhythm_intraday import (
    IntradayFactors,
    apply_intraday_rules,
    derive_intraday_factors,
    is_intraday_usable,
)
from aistock_agent.services.rhythm_position_ledger import (
    build_watch_points,
    render_causality,
)
from aistock_agent.services.rhythm_rebuilt_synthesis import run_synthesis
from aistock_agent.services.rhythm_rebuilt_validate import validate_synthesis
from aistock_agent.services.trend_reversal import detect_trend_reversal
from aistock_agent.state.schema import AgentState
from aistock_agent.utils.date import (
    CALENDAR_MAX_YEAR,
    add_trading_days,
    prev_trading_day,
    shanghai_today,
    trading_days_between,
)
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
SECTOR_LOOKBACK_NATURAL_DAYS = 130  # spec §5.4.1 T4：板块取数窗口（≥65 个交易日行）

# 事件近窗口交易日数（裁决 C4/A2）：前端按 `event_window_near_end_date` 切分平铺/折叠。
# add_trading_days 语义为"不含 d 向后推 n 个交易日"，故借助该函数取 NEAR_HINT_DAYS 个
# 交易日得到近窗口末日。无既有数值常量可复用（event_high_hint 仅按 importance 分级，
# 无日数阈值；event_calendar.HORIZON_TRADING_DAYS=4 是"含当日共 5 交易日"的分析窗偏移，
# 语义=再加 target 当日，与"近窗口末日"口径不同），此处定为单一取值源，防两处数字分叉。
NEAR_HINT_DAYS = 5

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


def _stage_from_report(report: object) -> Stage | None:
    """从节奏卡响应提取市场阶段（缺失/越界一律 None）。

    真实契约：`NodeApiClient._request` 已解包 `code==200` 信封，业务对象把 `content`
    放在顶层（对齐 rhythm_verification.py 的 `resp.get("content")`）。

    越界 stage 必须在此拦截：该值会流入 `RhythmEvidence.stage`（`Stage | None` 的
    Literal），一旦是 Node 侧回读的野值，将在构造时抛 ValidationError 打断整轮刷新。
    """
    if not isinstance(report, dict):
        return None
    content = report.get("content")
    if not isinstance(content, dict):
        return None
    evidence = content.get("evidence")
    if not isinstance(evidence, dict):
        return None
    stage = evidence.get("stage")
    if not isinstance(stage, str) or not stage or stage not in get_args(Stage):
        return None
    return cast("Stage", stage)


def _report_position_index(report: object) -> int | None:
    """从节奏卡响应提取 `content.rhythm_card.position_index`（0-4 整数）。

    越界 / 非整数 / 缺失一律 None（fail closed，不猜、不 clamp）。单一收口：
    `_inherit_basis_stage` 与 `_resolve_basis_card` 共用同一份校验，防两处规则漂移。
    """
    if not isinstance(report, dict):
        return None
    content = report.get("content")
    if not isinstance(content, dict):
        return None
    card = content.get("rhythm_card")
    if not isinstance(card, dict):
        return None
    raw_index = card.get("position_index")
    if (
        isinstance(raw_index, int)
        and not isinstance(raw_index, bool)
        and engine.LADDER_MIN <= raw_index <= engine.LADDER_MAX
    ):
        return raw_index
    return None


# midday 回退到收盘卡时的留痕文案（前端/审计据此区分「串联」与「回退」）
BASIS_FALLBACK_NOTE = "早盘卡缺失或没有有效仓位，改用收盘那次定的仓位"


async def _resolve_basis_card(
    slot: str, target_date: str
) -> tuple[dict[str, object] | None, str | None]:
    """按期次解析要沿用的上游卡（职责重定义 spec §4.1：串联继承链，非并行）。

    `after_close`（定档）→ `morning`（读 after_close，做事件修正）→
    `midday`（读 morning，做盘中修正；morning 不可用才回退 after_close）。

    - `morning`：读 `(target_date, after_close)`；
    - `midday`：**先**读 `(target_date, morning)`，其 `content.rhythm_card.
      position_index` 为 0-4 整数才采用；否则回退 `(target_date, after_close)`。
    - `after_close`：不继承，返回 `(None, None)`。

    返回 `(report, fallback_note)`：`fallback_note` 非 None 表示「先读的一环不可用、
    已回退到收盘卡」，调用方**必须**写入 `data_missing`（不许静默回退）。两环都读不到
    时返回 `(None, None)`，由调用方的「上一交易日的收盘仓位没取到」统一留痕（不重复报回退）。
    """
    if slot == "morning":
        return await node_api.get_rhythm_report(target_date, "after_close"), None
    if slot != "midday":
        return None, None
    morning = await node_api.get_rhythm_report(target_date, "morning")
    if _report_position_index(morning) is not None:
        return morning, None
    basis = await node_api.get_rhythm_report(target_date, "after_close")
    if basis is None:
        return None, None
    return basis, BASIS_FALLBACK_NOTE


def _inherit_basis_stage(
    slot: str, basis_response: object
) -> tuple[Stage, str, str | None, int | None, PositionLedger | None] | None:
    """morning/midday 的仓位沿用上游卡（对齐 AGENTS.md 节奏大师三时点契约）。

    仅 after_close 之外的两个时点继承；无上游卡时返回 None（调用方留痕）；上游
    stage 非法/为空或 evidence 残缺时返回 None（调用方本地重算，不另留痕）。
    G2：消灭「同日日历格 ice / 详情页 low」的自相矛盾。

    与 `_stage_from_report` 的分工：本函数负责「同日上游卡」的语义与文案，阶段校验
    统一委托给 `_stage_from_report`（单一收口，防两处规则漂移）。

    返回 `(stage, reason, position_text, position_index, position_ledger)`：
    仓位既已沿用，序号与账本也必须同源（2026-11-04 单一真相源）——
    morning/midday 用前一交易日 K 线重算仓位会造成同日三时点口径打架。
    上游卡是旧数据、无 `position_index` 时返回 None（调用方置灰，**不得按文案反推序号**）。

    基准来源由 `_resolve_basis_card` 决定：morning 的上游是 after_close，midday 的
    上游是 morning（缺失才回退 after_close）。文案据上游卡自身的 `refresh_slot`
    区分「收盘卡」与「盘前卡」，避免 midday 把盘前卡说成收盘卡。
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
    stage = _stage_from_report(basis_response)
    if stage is None:
        return None
    basis_date = content.get("basis_date")
    basis_label = "盘前判断" if content.get("refresh_slot") == "morning" else "收盘判断"
    reason = str(evidence.get("stage_reason") or "")
    position_text: str | None = None
    position_index: int | None = _report_position_index(basis_response)
    position_ledger: PositionLedger | None = None
    basis_card = content.get("rhythm_card")
    if isinstance(basis_card, dict):
        band = basis_card.get("position_band")
        if isinstance(band, dict):
            text = band.get("text")
            position_text = text if isinstance(text, str) and text else None
        raw_ledger = basis_card.get("position_ledger")
        if isinstance(raw_ledger, dict):
            try:
                position_ledger = PositionLedger.model_validate(raw_ledger)
            except ValidationError:
                position_ledger = None
    return (
        stage,
        f"沿用{basis_label}（{basis_date or '—'}）：{reason}",
        position_text,
        position_index,
        position_ledger,
    )


def _morning_event_item(
    *,
    event_item: LedgerItem,
    canonical: LedgerItem,
    delta: int,
    clamped: bool,
    revoked: bool,
    net: int,
) -> LedgerItem:
    """按盘前事件修正改写事件账本行（其余 3 行原样继承，`net == 0` 时整行不动）。

    - `delta`：相对「撤销临近 +1 后的基准」的事件方向调整量；
    - `net`：相对**沿用序号**的净变化（含撤销临近 +1 带来的 −1）。

    未收敛时取 `engine.event_ledger_item` 的规范文案（盘前事件数据已更新，只改
    judgement 会出现「value_text 说未落档、judgement 说 +1」的自相矛盾）；方向调整
    被 ±1 截断时按实调量改写；发生撤销时补一句净变化，避免「调整量」与账本 `adjust`
    对不上。
    """
    if not clamped:
        item = canonical
    else:
        item = event_item.model_copy(update={
            "value_text": canonical.value_text,
            "judgement": "事件 +1" if delta > 0 else "事件 -1",
            "effect": f"{delta:+d}（盘前事件修正，受 ±1 限制）",
            "status": "supporting" if delta > 0 else "blocking",
        })
    if revoked:
        net_text = f"{net:+d}" if net else "0"
        item = item.model_copy(update={
            "effect": f"{item.effect}；已撤销临近 +1（净 {net_text}）",
        })
    return item


def _apply_morning_event_adjustment(
    *,
    base_index: int,
    base_ledger: PositionLedger | None,
    event_d: int | None,
    event_result: str | None,
    mainline: dict[str, object],
    closes: list[float],
    candidates: list[CandidateOutlook],
) -> tuple[int, PositionLedger | None, str, str | None]:
    """morning 事件修正（spec §4.2）：在继承档位上按事件调档，其余维度不重算。

    返回 `(position_index, position_ledger, position_source, note)`：
    - `position_source`：`event_adjusted`（档位确有变化）/ `inherited_basis`（无修正）；
    - `note`：发生封顶 / 收敛 / 跳过时的留痕文案，由调用方写入 `data_missing`。

    **按继承账本的 `event_applied_kind` 三分支**（spec §4.1/§4.2 裁决）：

    - `"result"`：昨晚收盘定档已按这条结果算过 → 整体跳过，不得重复计；
    - `"approaching"`：收盘定档只有「临近事件 +1」（结果未知）→ **先撤销该 +1**
      （`start = 账本 event_off_index`，即 step2 结束、step3 之前的档位），再以 `start`
      为基准按结果重算。不撤销就会把「临近 +1」和「结果定档」叠在一起：利空落地仍停在
      原档，等于坏消息不降档；
    - `None`：未计入过 → 以沿用序号为基准，行为与首版一致。

    **幅度约束**：`|事件方向调整| ≤ 1`；但 `d==0 未落档 → 封顶 1` 是安全规则，
    **优先于 ±1 收敛、完整执行**（spec §4.2/§8.4 裁决）并在 `data_missing` 留痕。
    撤销临近 +1 不属于「方向调整」，故 morning 相对昨天的收盘判断的变化可达 2 档。
    """
    kind = base_ledger.event_applied_kind if base_ledger is not None else None
    if kind == "result":
        return (
            base_index,
            base_ledger,
            "inherited_basis",
            "盘前：事件结果在昨晚定档时已经算进去了，这里不重复调整",
        )
    # "approaching"：昨晚收盘定档的 +1 建立在「事件临近」上，结果未知；盘前先撤销再加。
    # 撤销基线取账本的 `event_off_index`（step2 结束、step3 之前的档位）：不能用
    # `沿用序号 − 1`，4 档上界会把 +1 整个吃掉（base3+主线1+事件1=5→4），此时
    # 「沿用序号 − 1」等于撤掉一个从未加上的 +1，后续结果调整基准偏低一档。
    revoked = kind == "approaching"
    note: str | None = None
    if revoked:
        start = base_ledger.event_off_index if base_ledger is not None else None
        if start is None:
            # 旧卡无该字段：按「沿用序号 − 1」近似（并在 data_missing 注明是近似）
            start = max(engine.LADDER_MIN, base_index - 1)
            note = "上一张卡缺少事件前的仓位记录，盘前撤销只能按一档估算"
        start = max(engine.LADDER_MIN, min(engine.LADDER_MAX, start))
    else:
        start = base_index
    gate_blocked = bool(base_ledger.gate_blocked) if base_ledger is not None else False
    mainline_state = str(mainline.get("state") or "") or None
    mainline_strength = mainline.get("strength")
    delta, reason = engine.event_adjustment(
        event_d=event_d,
        event_result=event_result,
        mainline_state=mainline_state,
        mainline_strength=mainline_strength,
        gate_blocked=gate_blocked,
        base_index=start,
    )
    capped = reason == engine.EVENT_CAP_REASON
    if capped:
        # 安全封顶优先于 ±1：完整执行到 min(档位, 1)，不得被幅度约束截断
        new_index = min(start, 1)
        clamped = False
        if new_index != base_index:
            note = "盘前发现有重要事件结果未出，仓位先压到低档"
    else:
        bounded = max(-1, min(1, delta))
        new_index = max(engine.LADDER_MIN, min(engine.LADDER_MAX, start + bounded))
        clamped = bounded != delta
        if clamped:
            note = "盘前事件想让仓位动得更多，但盘前最多只调一档，按一档处理"
    net = new_index - base_index
    ledger = base_ledger
    if ledger is not None:
        canonical = engine.event_ledger_item(
            event_d=event_d,
            event_result=event_result,
            gate_blocked=gate_blocked,
            block_reason="上一张卡的压制条件",
            mainline_state=mainline_state,
            mainline_strength=mainline_strength,
        )
        ledger = ledger.model_copy(update={
            "items": [
                _morning_event_item(
                    event_item=item, canonical=canonical,
                    delta=new_index - start, clamped=clamped,
                    revoked=revoked, net=net,
                )
                # 撤销路径即使净变化为 0 也要改写：盘前结果已落档，事件行的
                # value_text 若仍停在「d=1 事件临近」就是过期事实
                if item.key == "event" and (net or revoked) else item
                for item in ledger.items
            ],
            "adjust": ledger.adjust + net,
            "final_index": new_index,
            "event_applied_kind": engine.event_applied_kind(
                event_d=event_d,
                event_result=event_result,
                gate_blocked=gate_blocked,
                delta=delta,
                reason=reason,
            ),
        })
        # 档位变了 → 观察点必然变：按新档位重算（不得沿用上游卡的旧观察点）；
        # 档位未变时原样继承，避免用盘中数据无谓改写上游卡的反解结果。
        if net:
            ledger = ledger.model_copy(update={"watch_points": build_watch_points(
                ledger, mainline_facts=mainline, event_d=event_d,
                closes=closes, candidates=candidates,
            )})
    return new_index, ledger, ("event_adjusted" if net else "inherited_basis"), note


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


def _temperature_note(series: list[dict[str, Any]]) -> str | None:
    """证据区温度因子一行（T7 / 职责 spec §3）。

    温度是 `detect_stage` 的 sentiment 因子来源（判定用的是**近 N 日斜率**，不是当日
    绝对值），已从卡片主视觉移除但保留在证据链中，让用户能看懂「为什么判这个阶段」。

    口径三条：
    - 只给数值与差值，**不引分档词**（冰点/低迷/常温/活跃/亢奋 归情绪温度，节奏侧
      不得借用该套语汇，否则又回到「一名多义」）
    - 明确标「近 N 日」，不谎称是 7 日 —— 归档可能缺日，按实际样本数报
    - 序列为空 → None（前端不渲染该行，不伪造）
    """
    scores: list[float] = []
    for item in series:
        raw = item.get("score")
        if isinstance(raw, int | float):
            scores.append(float(raw))
    if not scores:
        return None
    if len(scores) == 1:
        return f"情绪温度 {scores[-1]:.1f}"
    delta = scores[-1] - scores[0]
    return f"情绪温度 {scores[-1]:.1f}（近 {len(scores)} 日 {delta:+.1f}）"


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


INTRADAY_UNAVAILABLE_NOTE = "盘中数据不可用，仓位这次没有跟着改"
INTRADAY_NO_WORSENING_NOTE = "盘中未见恶化"


async def _load_intraday_snapshot(target_date: str) -> dict[str, Any] | None:
    """midday 盘中快照：优先读 T0 归档（12:25），归档缺失才实时取数（spec §4.3）。

    归档存在即代表「12:30 决策时刻可拿到的口径」，直接读文件可避免同一端点重复
    请求；归档缺失（job 未跑 / 历史补跑）才回退 ``get_intraday_sectors()``，保证
    midday 不因归档缺失而失去盘中修正能力。两种失败都返回 None，由调用方按
    「盘中数据不可用」处理——不得默认按「未恶化」处理，也不得伪造中性输入。
    """
    path = (
        intraday_archive.archive_dir
        / f"{target_date}-{intraday_archive._SNAPSHOT_SUFFIX}.json"
    )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):  # 不存在 / 权限 / 坏 JSON / 编码错误
        payload = None
    if isinstance(payload, dict):
        return payload
    try:
        fetched = await node_api.get_intraday_sectors()
    except Exception:
        logger.warning("rhythm_master.intraday_fetch_failed", exc_info=True)
        return None
    return cast("dict[str, Any]", fetched) if isinstance(fetched, dict) else None



async def _compose_card(
    run_date: str, slot: str, target_date: str | None = None
) -> tuple[
    MasterRhythmCard,
    list[dict[str, object]],
    EventWindow,
    str | None,
    IntradayFactors | None,
]:
    """三时点证据流水线：返回 (MasterRhythmCard, rows, win, position_source, 盘中因子)。

    `run_date` 为运行时日期（scheduler 传入的 shanghai_today）；卡片 `basis_date`
    对外表示**证据日**（K 线末日），`target_date` 按 slot 由运行日推导（P1-6/G9）。
    显式传入 `target_date` 时覆盖推导（手动补跑 after_close 用：把基于历史某日
    数据算出的卡落回该日键，见 routes.trigger_rhythm_master）。

    rows 为 close 非空过滤后的 K 线行（供 _build_rhythm_card 复用，避免二次取数）；
    win 为当前窗口 EventWindow（事件分支/锚点来源）；
    position_source 为盘前事件修正 / 盘中降档判定出的档位来源（`event_adjusted` /
    `intraday_adjusted` / `intraday_unavailable` / `inherited_basis`），None = 未走
    继承链，由 `_build_rhythm_card` 按 stage_source 归类；
    intraday_factors 仅 midday 有值（spec §4.4，不可用时也返回含 null 的模型），
    其他时点为 None。
    """
    target_date = target_date or (
        add_trading_days(date_cls.fromisoformat(run_date), 1).isoformat()
        if slot == "after_close"
        else run_date
    )
    basis_inherit_note: str | None = None
    stage_inherit_note: str | None = None
    run_ymd = date_cls.fromisoformat(run_date).strftime("%Y%m%d")
    kline = (
        await node_api.get_index_kline(INDEX_CODE, days=KLINE_LOOKBACK, end_date=run_ymd) or []
    )
    rows = [r for r in kline if r.get("close") is not None]
    # 未完成 bar 剔除（spec §5.4.2）：盘中档剔除 trade_date==run_ymd 的行，
    # 防 15:00 后补跑把当日未完成 bar 污染趋势/反转判定。
    if slot in {"morning", "midday"}:
        rows = [r for r in rows if _normalize_ymd(r.get("trade_date")) != run_ymd]
    last_trade_date = _normalize_ymd(rows[-1].get("trade_date")) if rows else None
    # 证据日 = K 线末日（对外 basis_date 语义，P1-6/G9）；无 K 线时退回运行日。
    evidence_date = (
        f"{last_trade_date[0:4]}-{last_trade_date[4:6]}-{last_trade_date[6:8]}"
        if last_trade_date
        else run_date
    )
    # P0-2/G4 分槽门禁：after_close 的 basis 必须是"当日 K 线到位"的交易日；
    # morning/midday 的 basis 是运行日（盘中当日 bar 天然未出），不设该门禁。
    basis_gate = slot == "after_close" and (
        last_trade_date is None or last_trade_date != run_ymd
    )
    kline_short = len(rows) < MIN_KLINE_ROWS
    if kline_short:
        logger.warning("rhythm_master.kline_insufficient n=%s basis=%s", len(rows), run_date)
    closes = [float(cast("float", r["close"])) for r in rows[-65:]]
    # Tushare index_daily amount 千元 → 亿元（engine 单位契约；2026-09-05 核实修复：
    # Node /internal/index/:code/kline 此前丢弃 vol/amount，恒 null → 量能伪分支）
    amounts = [
        _amount_yi(
            float(cast("float", r["amount"])) if r.get("amount") is not None else None
        )
        for r in rows[-120:]
    ]
    fg = (await node_api.get_fear_greed() or {}).get("index")
    # 需求 2：事件提前展示（不限 5 交易日）。单次拉全量展示窗（到 CALENDAR_MAX_YEAR
    # 年末截断，G3 越年留痕），再切 ≤5 交易日分析子窗喂 event_confirm/event_d/锚点/
    # 分支——A2 裁决：单请求超集 + 交易日差口径切分，HTTP 仍 1 次/卡/时点，且分析
    # 链路永不接触全量（"临近=证据"语义不被远期事件污染）。
    win_full = await load_event_timeline(target_date, horizon_days=None)
    win = split_analysis_window(win_full.events, target_date)
    # getattr 兜底：兼容只声明 events 的旧测试替身（缺这两键时按 False）。
    win.source_missing = getattr(win_full, "source_missing", False)
    win.calendar_uncovered = getattr(win_full, "calendar_uncovered", False)
    _, sentiment_scores, _, _ = _load_sentiment_series(days=7)

    # 主线判定（spec §5.1，确定性；失败/数据不足 → unavailable + 留痕，H5）
    mainline: dict[str, object] = {
        "state": "unavailable", "name": None, "strength": None,
        "excess": None, "data_date": None, "attention": "",
        "breakdown": None, "nav": None,
    }
    mainline_notes: list[str] = []
    # 主线候选排名（因果输出 spec §6.4）：供变奏观察点的候选池展示。必须在主线判定
    # 块之前初始化——`valid`/`index_pct_chgs` 只在判定分支内可见，块外不得引用。
    mainline_candidates: list[CandidateOutlook] = []
    if not settings.rhythm_mainline_enabled:
        mainline_notes.append("主线判定未启用")
    else:
        ok, cands = load_mainline_candidates()
        if not ok:
            mainline_notes.append("主线板块清单缺失")  # 降级 1：候选清单缺失
        else:
            index_resp = await node_api.get_ths_index_map()
            index_map = index_resp if isinstance(index_resp, list) else []
            # 板块表同码重复 → 保留首次（确定性：以接口声明序为准），并留痕。
            # 原先的字典推导是静默 last-wins（结果随接口返回序漂移，不可复现）。
            idx_by_code: dict[str, str] = {}
            dup_index_codes = 0
            for i in index_map:
                code_i = str(i.get("ts_code"))
                if code_i in idx_by_code:
                    dup_index_codes += 1
                    continue
                idx_by_code[code_i] = str(i.get("name") or "")
            if dup_index_codes:
                mainline_notes.append(
                    f"板块列表里有 {dup_index_codes} 个重复项，已自动去重"
                )
            start = (
                date_cls.fromisoformat(evidence_date)
                - timedelta(days=SECTOR_LOOKBACK_NATURAL_DAYS)
            ).isoformat()
            valid: list[dict[str, object]] = []
            code_skipped = 0
            name_skipped = 0
            thin_skipped = 0
            fetch_failed = 0
            for c in cands:
                code = str(c.get("tag_code") or "")
                if code not in idx_by_code:
                    code_skipped += 1
                    continue  # 降级 3：code 不在板块表 → 剔除
                if not candidate_name_matches(c, idx_by_code[code]):
                    name_skipped += 1
                    continue  # §5.10.3 名称不一致 → 剔除（防"代码存在但语义错"）
                rows_b = await node_api.get_ths_daily_range(code, start, evidence_date)
                if rows_b is None:
                    fetch_failed += 1
                    continue  # 降级 5：取数失败（≠ 数据不足，硬约束 12 禁混写归因）
                pk = [p for p in rows_b if p.get("pct_chg") is not None]
                if len(pk) < MA20_MIN_BARS:
                    thin_skipped += 1
                    continue  # 降级 4：pct_chg 序列不足 → 剔除
                valid.append({
                    **c,
                    "pct_chgs": [float(cast("float", p["pct_chg"])) for p in pk],
                    "last_trade_date": _normalize_ymd(rows_b[-1].get("trade_date"))
                    if rows_b else None,
                })
            # 已过入库门槛但算不出超额的候选（len(pct_chgs) <= RET_WINDOW）：单独计数，
            # 避免被判定层的"候选齐备但无清晰主线"掩盖真实根因（硬约束 12 同族）。
            unscorable = sum(
                1
                for c in valid
                if len(cast(list[float], c["pct_chgs"])) <= RET_WINDOW
            )
            mainline_notes.extend(
                build_mainline_notes(
                    valid_count=len(valid),
                    min_candidates=MIN_CANDIDATES,
                    code_skipped=code_skipped,
                    name_skipped=name_skipped,
                    thin_skipped=thin_skipped,
                    fetch_failed=fetch_failed,
                    unscorable=unscorable,
                )
            )
            if len(valid) >= MIN_CANDIDATES:
                index_pct_chgs = [
                    float(cast("float", r["pct_chg"]))
                    for r in rows if r.get("pct_chg") is not None
                ]
                mainline = judge_mainline(valid, index_pct_chgs, evidence_date)
                # 与 judge_mainline 同源（同一 _pool_best），只做展示截断，不改判据
                mainline_candidates = rank_candidates(valid, index_pct_chgs)
                if mainline.get("state") == "established":
                    winner = next(
                        (c for c in valid if c.get("name") == mainline.get("name")), None
                    )
                    if winner:
                        w_last = winner.get("last_trade_date")
                        # H2：主线 data_date 必须 == 证据日（K 线末日），不等 → unavailable
                        if w_last and w_last != _normalize_ymd(evidence_date):
                            mainline_notes.append(
                                f"主线数据没跟上（最新只到 {w_last}，应该是 {evidence_date}）"
                            )
                            mainline = {
                                "state": "unavailable", "name": None, "strength": None,
                                "excess": None, "data_date": None,
                                "attention": (
                                    f"主线数据没跟上（最新只到 {w_last}，应该是 {evidence_date}）"
                                ),
                                "breakdown": None, "nav": None,
                            }
                        else:
                            wnav = nav_from_pct(winner["pct_chgs"])  # type: ignore[arg-type]
                            brk = detect_breakdown(closes, wnav)
                            label = str(winner.get("mainline") or "")
                            mainline = {
                                **mainline,
                                "nav": wnav,
                                "breakdown": brk["mainline_breakdown"],
                                "mainline_label": label,
                            }

    breadth: dict[str, Any] | None = None
    snapshot_missing = False
    # G1：宽度证据必须与 K 线证据日同源（此前 get_last_close_snapshot() 取
    # 「严格早于今天」的最近交易日 → after_close(周五) 实际取周四宽度）。
    if last_trade_date is None:
        snapshot_missing = True
    else:
        snap = await node_api.get_close_snapshot(last_trade_date)
        if isinstance(snap, dict):
            # cast：快照 breadth 为外部 JSON 字典；保留 | None —— 「breadth 键缺失」
            # 时必须仍是 None，不能伪装成非空（下游 engine 形参本身即接受 None）。
            breadth = cast("dict[str, Any] | None", snap.get("breadth"))
        else:
            snapshot_missing = True

    stage_source: StageSource | None = None
    if kline_short:
        stage: Stage | None = None
        stage_reason = "指数K线不足20根，趋势/量能判定不可用"
    elif basis_gate:
        stage = None
        stage_reason = "基准日无当日K线，趋势/量能判定不适用"
    else:
        # X2（spec §5.12.2 / R11）：证据中性时 detect_stage 会走兜底「沿用前阶段」，但生产调用点
        # 固定传 prev_phase=None 使该兜底失效 → stage=None → level/score/phase 整条热度轴消失
        # （2026-09-19 生产实测：index_breakdown=true 而三键全 null）。改为读「前一交易日
        # after_close 卡」的市场阶段传入；不改任何判据。
        # 作用域收口（controller 裁决）：仅 after_close 查询——morning/midday 已有同日 after_close
        # 沿用（下方 P0-2/G2 块），此处再查前一交易日会多一次取数并破坏既有 await 次数断言。
        prev_resp: object = None
        prev_stage: Stage | None = None
        if slot == "after_close":
            prev_date = prev_trading_day(date_cls.fromisoformat(run_date)).isoformat()
            prev_resp = await node_api.get_rhythm_report(prev_date, "after_close")
            # D2 双闸（2026-11-03）：时效 + 拒绝二手阶段，防止错卡沿链传播
            prev_stage = ev.resolve_prev_phase(
                prev_resp,
                run_date=run_date,
                max_gap_days=settings.rhythm_prev_phase_max_gap_days,
            )
        stage, stage_reason = ev.detect_stage(
            breadth=breadth, closes=closes, amounts=amounts,
            sentiment_scores=sentiment_scores,
            fg=fg if isinstance(fg, int | float) else None,
            prev_phase=prev_stage,
        )
        # 判定路径（问题 1）：只有在本地依当日证据判出方向时才算 computed，
        # 中性兜底沿用前一交易日卡的一律标记，供前端区分"沿用"与"判定"。
        if stage is not None:
            stage_source = (
                "inherited_prev_day"
                if stage_reason == ev.INHERIT_PREV_PHASE_REASON
                else "computed"
            )
        # 降级留痕：仅当 after_close 且「本地无阶段可归」且「前卡也拿不到阶段」时才提示——避免每卡
        # 常驻噪音（对齐 2026-09-14 裁决：已知空置字段不写入 data_missing）。两类根因分开措辞
        # （硬约束 12）：取数失败 ≠ 卡存在但阶段非法。
        if slot == "after_close" and stage is None and prev_stage is None:
            stage_inherit_note = (
                "上一交易日的收盘卡没读到，市场阶段沿用了更早的判断"
                if prev_resp is None
                else "上一交易日的收盘卡没有有效阶段，市场阶段沿用了更早的判断"
            )
    # 事件档位（需求⑤）：d 与 next_event_anchor 同函数同源。必须在继承块与构造 evidence
    # 之前算好——盘前事件修正（spec §4.2）与仓位判定（derive_position_index step3）要用
    # 同一对 d/result。
    win_highs = getattr(win, "high_events", None) or []
    event_d: int | None = None
    event_result: str | None = None
    if win_highs:
        first_high = win_highs[0]
        try:
            event_d = trading_days_between(
                date_cls.fromisoformat(target_date),
                date_cls.fromisoformat(str(first_high.get("date") or "")),
            )
        except ValueError:
            event_d = None
        raw_result = first_high.get("result")
        event_result = str(raw_result) if raw_result is not None else None

    # P0-2/G2：morning/midday 仓位**逐级**沿用上游卡（spec §4.1 串联继承链）：
    # after_close（定档）→ morning（读 after_close，做事件修正）→
    # midday（读 morning，做盘中修正；morning 不可用才回退 after_close）。
    # D1（2026-11-03）：去掉 `and stage is not None` —— 本地判空时恰恰最需要基准兜底，
    # 否则盘前/午间卡恒为 stage=null，前端只能回退旧卡。
    inherited_position_text: str | None = None
    inherited_position_index: int | None = None
    inherited_position_ledger: PositionLedger | None = None
    inherited_basis = False
    position_source_hint: str | None = None
    if slot in {"morning", "midday"}:
        basis_resp, fallback_note = await _resolve_basis_card(slot, target_date)
        if fallback_note:
            basis_inherit_note = fallback_note
        inherited = _inherit_basis_stage(slot, basis_resp)
        if inherited is not None:
            (
                stage,
                stage_reason,
                inherited_position_text,
                inherited_position_index,
                inherited_position_ledger,
            ) = inherited
            stage_source = "inherited_basis"
            inherited_basis = True
        elif basis_resp is None:
            basis_inherit_note = "上一交易日的收盘仓位没取到，这次没有沿用"

    # spec §4.2：morning 只允许「事件 ±1」修正仓位。趋势 / 量能 / 宽度维度一律继承
    # 上游卡（09:00 无当日盘面数据），**不得**调 derive_position_index 重算。
    morning_adjust_note: str | None = None
    if slot == "morning" and inherited_basis:
        if inherited_position_index is None:
            # 旧卡无仓位 → 不修正、保持 null（前端灰格），只留痕
            morning_adjust_note = "上一张卡没有有效仓位，盘前的事件修正没有做"
        else:
            (
                inherited_position_index,
                inherited_position_ledger,
                position_source_hint,
                morning_adjust_note,
            ) = _apply_morning_event_adjustment(
                base_index=inherited_position_index,
                base_ledger=inherited_position_ledger,
                event_d=event_d,
                event_result=event_result,
                mainline=mainline,
                closes=closes,
                candidates=mainline_candidates,
            )

    # spec §4.3 / §4.4：midday 盘中修正（**只降不升**）。盘中数据源优先读 12:25 归档，
    # 缺失才实时取数（正常路径只有一次请求）。无论可用与否，盘中因子都要落库——这是
    # 阈值未来可回测的唯一自变量来源。
    intraday_factors: IntradayFactors | None = None
    midday_adjust_note: str | None = None
    if slot == "midday":
        snapshot_payload = await _load_intraday_snapshot(target_date)
        intraday_factors = derive_intraday_factors(
            snapshot_payload,
            closes,
            mainline_name=cast("str | None", mainline.get("name")),
            version=settings.rhythm_intraday_thresholds_version,
        )
        if not is_intraday_usable(snapshot_payload, intraday_factors):
            # `availability.state != "available"` / 点位缺失 / `intr_close <= 0` 一律
            # 不修正，且**必须留痕**——不得默认按「未恶化」处理（spec §4.3）。
            midday_adjust_note = INTRADAY_UNAVAILABLE_NOTE
            if inherited_basis:
                position_source_hint = "intraday_unavailable"
        elif inherited_basis and inherited_position_index is not None:
            base_index = inherited_position_index
            new_index, rules = apply_intraday_rules(
                base_index, intraday_factors, snapshot_payload,
            )
            intraday_factors = intraday_factors.model_copy(
                update={"triggered_rules": rules}
            )
            if new_index != base_index:
                delta = new_index - base_index
                ledger = inherited_position_ledger
                if ledger is not None:
                    # 档位变了 → adjust / final_index 同步；观察点必须按新档位重算
                    ledger = ledger.model_copy(update={
                        "adjust": ledger.adjust + delta,
                        "final_index": new_index,
                    })
                    ledger = ledger.model_copy(update={"watch_points": build_watch_points(
                        ledger, mainline_facts=mainline, event_d=event_d,
                        closes=closes, candidates=mainline_candidates,
                    )})
                    inherited_position_ledger = ledger
                inherited_position_index = new_index
                position_source_hint = "intraday_adjusted"
                midday_adjust_note = f"盘中走弱（{len(rules)} 个信号），仓位下调"
            elif rules:
                # 规则命中但已在下限（如 0 档）：如实记录命中，不冒充「未恶化」
                midday_adjust_note = "盘中走弱，但仓位已经在最低，没有再降"
            else:
                midday_adjust_note = INTRADAY_NO_WORSENING_NOTE
        elif inherited_basis:
            midday_adjust_note = "上一张卡没有有效仓位，盘中的修正没有做"

    # 确信度 / 事件锚点必须用**最终** stage（继承后）计算，不可用继承前值
    event_confirm = _event_confirm(win.events)
    volume_direction = _volume_confirm(amounts, stage)
    cert, cert_reason = ev.detect_certainty(
        event_confirm=event_confirm, volume_direction=volume_direction,
        stage=stage, breadth=breadth,
    )
    anchors = ev.build_event_anchors(win.events)

    # 仓位唯一真相源（因果输出 spec §3）：序号 / 文案 / 账本全部来自同一次
    # derive_position_index。morning/midday 继承上游卡仓位时直接沿用其序号与账本，
    # 不重算——否则盘中会用前一交易日 K 线得出第二套仓位（双真相源）。旧卡无
    # position_index → 序号为 None（前端灰格），不得按文案反推。
    position_index: int | None = None
    position_ledger: PositionLedger | None = None
    event_cap_note: str | None = None
    if inherited_basis:
        # 继承路径不重算：观察点已在上游卡账本里（morning 事件修正后按新档位重算），
        # 连同账本一起沿用（否则同日三时点会算出互相矛盾的观察点）。
        position_index = inherited_position_index
        position_ledger = inherited_position_ledger
    else:
        position_index, position_ledger = engine.derive_position_index(
            index_closes=closes, mainline=mainline,
            event_d=event_d, event_result=event_result, breadth=breadth,
        )
        # 变奏观察点：纯函数不回填，由本处写回账本（因果输出 spec §6）。event_d=None
        # 时不产出事件项，不得用 0 伪装「事件当日」。
        watch_points = build_watch_points(
            position_ledger, mainline_facts=mainline, event_d=event_d,
            closes=closes, candidates=mainline_candidates,
        )
        position_ledger = position_ledger.model_copy(update={"watch_points": watch_points})

    # 事件当期（d=0）结果未落档 → 档位封顶 1（spec §5.2.2 step3 安全线）。
    # 为什么要有这道「出口闸」：after_close 定档时事件可能尚未入库（盘后财报等），
    # morning/midday 沿用基准档位时若不过闸门，就会因「沿用」被放过。11:30 / 13:00
    # 的预期差扫描也可能让 midday 才看到当天的事件——所以必须按**本卡自己**的
    # event_d/event_result 在档位层面兜一次，而不是在文案层面反推档位
    # （远端 be1c558 曾用 _POSITION_TEXT_RANK 把文案映射回序号，那正是本次重构
    #   拆掉的反向推断，已弃用）。
    if (event_d == 0 and event_result not in engine.EVENT_RESULT_ENUM
            and position_index is not None and position_index > 1):
        capped_index = 1
        if position_ledger is not None:
            position_ledger = position_ledger.model_copy(update={
                "adjust": position_ledger.adjust + (capped_index - position_index),
                "final_index": capped_index,
                "watch_points": build_watch_points(
                    position_ledger, mainline_facts=mainline, event_d=event_d,
                    closes=closes, candidates=mainline_candidates,
                ),
            })
        event_cap_note = "事件当天结果还没出来，仓位先往低里压，等结果出来再调整"
        position_index = capped_index
    position: PositionBand | None = None
    if position_index is not None:
        action, direction = engine.position_action(position_index)
        position = PositionBand(
            text=engine.POSITION_LADDER[position_index],
            action=action, direction=direction, index=position_index,
        )

    missing: list[str] = list(mainline_notes)
    if kline_short:
        missing.append("指数K线不足")
    if basis_gate:
        missing.append("当天没有 K 线数据（非交易日或数据还没到）")
    if snapshot_missing:
        missing.append("涨跌家数数据缺失（当天没有收盘统计）")
    if basis_inherit_note:
        missing.append(basis_inherit_note)
    if stage_inherit_note:
        missing.append(stage_inherit_note)
    if morning_adjust_note:
        missing.append(morning_adjust_note)
    if midday_adjust_note:
        missing.append(midday_adjust_note)
    if event_cap_note:
        missing.append(event_cap_note)
    evidence = RhythmEvidence(
        stage=stage, stage_reason=stage_reason, stage_source=stage_source,
        certainty=cert, certainty_reason=cert_reason,
        position=position, position_ledger=position_ledger,
        event_anchors=anchors, data_missing=missing,
    )
    synthesis = await run_synthesis(evidence, mainline_facts=mainline)
    synthesis_ok = synthesis is not None and validate_synthesis(synthesis, evidence)
    return (
        MasterRhythmCard(
            basis_date=evidence_date, target_date=target_date, refresh_slot=slot,
            evidence=evidence, synthesis=synthesis if synthesis_ok else None,
            synthesis_available=synthesis_ok, mainline_facts=mainline,
            inherited_position_text=inherited_position_text,
        ),
        rows,
        win,
        position_source_hint,
        intraday_factors,
    )


def _near_window_end_date(target_date: str) -> str | None:
    """近窗口（NEAR_HINT_DAYS=5 交易日）末日；越年/异常 → None（裁决 C4）。

    前端按 `event.date <= near_end_date` 切分平铺/折叠；越年（超出 chinese_calendar
    覆盖 CALENDAR_MAX_YEAR=2026）→ None 时前端近窗全部平铺。与 event_high_hint 阈值
    同源数值（NEAR_HINT_DAYS）。
    """
    try:
        target = date_cls.fromisoformat(target_date)
        if target.year > CALENDAR_MAX_YEAR:
            return None
        end = add_trading_days(target, NEAR_HINT_DAYS)
        if end.year > CALENDAR_MAX_YEAR:
            return None
        return end.isoformat()
    except (ValueError, TypeError):
        return None


# 与 A 股节奏无关的事件标题特征（spec §11-4）：只滤「明显无关」，不做语义分类，
# 避免误杀真正有用的境外宏观（美联储议息 / 美国 CPI 等仍在窗内）。
_IRRELEVANT_EVENT_KEYWORDS = (
    "美股感恩节", "美股圣诞节", "美股独立日",
    "韩国高考", "韩国股市", "日本股市休市",
)
# 交割日同一天只留一条（delivery 优先于 macro）
_DELIVERY_TITLE_KEY = "交割日"


def _filter_event_window(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """事件窗去重 + 噪音过滤（spec §11-4）。

    - 噪音：境外休市 / 他国交易制度类 → 剔除；
    - 同日同题：按 (date, title) 去重；
    - 同日交割日：只留一条，`type=delivery` 优先于 `macro` 的同义条；
    - 输出按日期升序（同日保持原有相对顺序），卡片按时间顺读。
    """
    kept: list[dict[str, Any]] = []
    delivery_at: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    for item in events:
        title = str(item.get("title") or "")
        date_s = str(item.get("date") or "")
        if any(kw in title for kw in _IRRELEVANT_EVENT_KEYWORDS):
            continue
        key = (date_s, title.strip())
        if key in seen:
            continue
        seen.add(key)
        if _DELIVERY_TITLE_KEY in title:
            if date_s in delivery_at:
                idx = delivery_at[date_s]
                if item.get("type") == "delivery" and kept[idx].get("type") != "delivery":
                    kept[idx] = item  # delivery 优先，替换先前留下的 macro 同义条
                continue
            delivery_at[date_s] = len(kept)
        kept.append(item)
    kept.sort(key=lambda e: str(e.get("date") or ""))  # 稳定排序，同日保序
    return kept


async def _build_delta_payload(
    *,
    card: MasterRhythmCard,
    slot: str,
    basis_date: str,
    mainline_facts: dict[str, Any] | None,
    rows: list[dict[str, object]],
) -> tuple[dict[str, Any] | None, str | None]:
    """取上一条卡 → 算 delta → 生成叙事。永不抛异常，缺什么就留痕什么。"""
    if not settings.rhythm_delta_enabled:
        return None, None
    try:
        prev_date, prev_slot, allow_fallback = delta_svc.resolve_prev(
            target_date=card.target_date, slot=slot, basis_date=basis_date,
        )
        prev_resp = await node_api.get_rhythm_report(prev_date, prev_slot)
        fallback = False
        if not isinstance(prev_resp, dict) and allow_fallback:
            prev_resp = await node_api.get_rhythm_report(card.target_date, "after_close")
            fallback = True
        prev_content = prev_resp.get("content") if isinstance(prev_resp, dict) else None
        if not isinstance(prev_content, dict):
            card.evidence.data_missing.append("没有上一条节奏卡可比，这次不显示变化")
            return None, None

        prev_basis = str(prev_content.get("basis_date") or "")
        lag = None
        try:
            lag = trading_days_between(
                date_cls.fromisoformat(prev_basis), date_cls.fromisoformat(basis_date),
            )
        except (ValueError, TypeError):
            lag = None
        if lag is None:
            # 越出交易日历覆盖范围（chinese_calendar 仅到 2026 年底）→ 不得静默当 0 天
            card.evidence.data_missing.append("交易日历算不出上一条卡距今几天，这次不显示变化")
            return None, None

        review = _build_review(prev_content, rows) if slot == "after_close" else None
        delta = delta_svc.build_delta(
            prev_content=prev_content, card=card, mainline_facts=mainline_facts,
            lag_days=lag, slot_fallback=fallback, review=review,
        )
        if delta is None:
            return None, None
        if not (prev_content.get("rhythm_card") or {}).get("position_ledger"):
            card.evidence.data_missing.append("上一条卡的判断依据缺失，只能对比仓位变化")
        if lag > 1:
            card.evidence.data_missing.append(f"上一条卡是 {lag} 个交易日前的")

        narrative = await delta_narrative.run(delta)
        return delta.model_dump(), narrative
    except Exception:
        logger.warning("rhythm_master.delta_failed", exc_info=True)
        card.evidence.data_missing.append("这次没能对比出上一条卡的变化")
        return None, None


def _build_review(
    prev_content: dict[str, Any], rows: list[dict[str, object]]
) -> DeltaReview | None:
    """昨日复盘：昨日档位 + 昨日上证涨跌幅。取不到就返回 None（整行不渲染）。"""
    if len(rows) < 2:
        return None
    try:
        close = float(rows[-1]["close"])  # type: ignore[arg-type]
        prev_close = float(rows[-2]["close"])  # type: ignore[arg-type]
    except (KeyError, TypeError, ValueError):
        return None
    if prev_close == 0:
        return None
    pct = (close - prev_close) / prev_close * 100
    band = ((prev_content.get("rhythm_card") or {}).get("position_band") or {}).get("text") or ""
    return DeltaReview(
        prev_target_date=str(prev_content.get("target_date") or ""),
        prev_position_text=str(band),
        index_pct_chg=round(pct, 2),
        up_ratio=None,  # 宽度口径需与 K 线同日，本批未接线 → 整半句不渲染（spec §8.2）
    )


def _build_rhythm_card(
    card: MasterRhythmCard, win: EventWindow, rows: list[dict[str, object]],
    mainline: dict[str, object] | None = None,
    *,
    position_source_hint: str | None = None,
    intraday_factors: IntradayFactors | None = None,
    delta_payload: dict[str, Any] | None = None,
    delta_narrative: str | None = None,
) -> dict[str, object]:
    """按前端 RhythmCard 契约构造 rhythm_card（2026-09-05 裁决 + 本批主线/仓位接线）。

    - score 由 level 派生同源（score=level_idx×20，见 STAGE_TO_LEVEL）；
    - branches 由 rhythm_engine 确定性生成（technical + event），不靠 LLM；
    - `position_band.text` 唯一来源 = `evidence.position.index` 查 `POSITION_LADDER`
      （2026-11-04 单一真相源：不再有 inherited/derive_position_text 三分支）；
      `position_index` / `position_ledger` / `position_source` 同期透出；
      `causality_text` 与账本 `watch_points` 同源于同一份 PositionLedger（T8）；
    - `position_source_hint`：由 `_compose_card` 判定的档位来源（盘前事件修正 / 盘中
      降档是否真的改了档位），仅用于 morning/midday 的 inherited_basis 分支；缺省
      None = 按 stage_source 归类（旧调用点行为不变）；
    - `intraday_factors`：盘中因子（spec §4.4），仅 midday 非 None；其余时点写 None，
      保证键结构稳定、历史样本可回测；
    - `delta_payload` / `delta_narrative`：跨日变更归因（spec §10），开关关闭时均为 None；
    - `mainline_snapshot`：本卡主线事实快照，落库供下一张卡算 delta；
    - `phase_evidence.technical` / `event_high_hint` 为既有契约槽的确定性生产者；
    - `event_window` 已接线（A2）：由 `engine.project_event_window` 投影**全部**已接入
      事件（含 medium），供前端事件日历渲染；
    - `temperature_series` 已由 C1（2026-11-03）接线：读 sentiment 归档近 7 个交易日
      序列透出到卡片字段（归档缺失/为空时返回空数组，不伪造；前端 v-if 自然隐藏），
      不再属缺失清单（对齐 spec §2.2 / G4）。
    """
    from aistock_agent.schemas.rhythm_master import STAGE_TO_LEVEL  # F3 常量，score 派生同源

    # C1（2026-11-03）：temperature_series 接线。此前该字段恒为空数组（spec §7 S4/S5 已知空置），
    # 前端 v-if 渲染早已就绪。归档缺失/为空时返回空数组，不伪造（前端自然隐藏该区块）。
    temp_series, _, _, _ = _load_sentiment_series(days=7)

    # DEPRECATED（2026-11-04，职责重定义 T4）：level / score 仅为未升级客户端兼容保留产出，
    # 前端已零引用。切勿置 None 或删除 —— uni-app 三端，App 用户不升级即长期跑旧版，
    # 小程序有审核延迟，置 null 会让未升级用户的日历整片变灰。语义废弃靠「前端零引用」
    # 完成，接口上保留原值。新代码请用 position_index / position_ledger。
    level_entry = STAGE_TO_LEVEL.get(card.evidence.stage)
    level = level_entry["level"] if level_entry else None
    score = level_entry["score"] if level_entry else None
    closes = [float(cast("float", r["close"])) for r in rows[-65:] if r.get("close") is not None]
    amounts = [
        _amount_yi(
            float(cast("float", r["amount"])) if r.get("amount") is not None else None
        )
        for r in rows[-120:]
    ]
    highs = [
        float(cast("float", r["high"])) if r.get("high") is not None else None
        for r in rows[-120:]
    ]
    lows = [
        float(cast("float", r["low"])) if r.get("low") is not None else None
        for r in rows[-120:]
    ]
    missing = list(card.evidence.data_missing)
    data_missing_container: list[str] = list(card.evidence.data_missing)
    # §5.7 预算裁决：总数 ≤3（条目数）；两个来源互斥使用，被让位方留痕。
    # 理由：技术分档是"互斥全覆盖"整体（截断会破坏覆盖性与验证器依赖）→ 优先整体保留；
    # 技术不可用且有关键事件时才让位给事件三情景。
    # 注意：用 getattr 兼容只声明 events/source_missing 的旧测试替身（无 high_events 属性）。
    win_highs_for_budget = getattr(win, "high_events", None) or []
    # 分支接档位（因果输出 spec §7）：position_action 必须从本卡当前档位出发，
    # 闸门命中时 bullish 分支被压制为「暂不加」——否则 0 档卡会写出「加仓」，
    # 承诺一个 derive_position_index 自己不会执行的动作。缺失 → None，分支回退旧静态文案。
    branches: list[dict[str, object]] = []
    branch_from_index = card.evidence.position.index if card.evidence.position else None
    branch_gate_blocked = (
        card.evidence.position_ledger.gate_blocked
        if card.evidence.position_ledger
        else False
    )
    try:
        tech_branches = engine.build_technical_branches(
            closes=closes, highs=highs, lows=lows, amounts=amounts,
            data_missing=data_missing_container,
            from_index=branch_from_index, gate_blocked=branch_gate_blocked,
        )
        if tech_branches:
            branches = list(tech_branches[:3])
            # 条数上限属实现细节，对用户无价值 —— 不再写入 data_missing（spec §11-5）
        else:
            for e in win_highs_for_budget[:1]:
                branches.extend(
                    engine.build_event_branch(
                        e, card.target_date,
                        from_index=branch_from_index, gate_blocked=branch_gate_blocked,
                    )
                )
            branches = branches[:3]
    except Exception:
        logger.warning("rhythm_master.rhythm_card_branches_failed", exc_info=True)
        branches = []
        data_missing_container.append("这次没有生成情景分支")
    # §5.7 两者皆不可得 → branches = [] 并留痕：正常路径（非异常）空分支同样留痕，与 except 路径去重
    if not branches and "这次没有生成情景分支" not in data_missing_container:
        data_missing_container.append("这次没有生成情景分支")
    missing.extend(m for m in data_missing_container if m not in missing)

    # 主线/技术佐证（spec §5.4.2 detect_breakdown 单一判据）
    mainline_facts = mainline or {}
    tech = detect_breakdown(
        closes, cast("list[float] | None", mainline_facts.get("nav"))
    )
    if tech["insufficient"]:
        missing.append("MA 技术位数据不足")
    # ② 趋势反转（spec §5.4.2；已完成 bar 不足 → 视为未确认 + 留痕，H5）
    rev_closes, rev_opens, rev_highs, rev_lows, rev_amounts = [], [], [], [], []
    for r in rows[-120:]:
        c = r.get("close")
        if c is None:
            continue
        rev_closes.append(float(cast("float", c)))
        rev_opens.append(
            float(cast("float", r["open"])) if r.get("open") is not None else None
        )
        rev_highs.append(
            float(cast("float", r["high"])) if r.get("high") is not None else None
        )
        rev_lows.append(
            float(cast("float", r["low"])) if r.get("low") is not None else None
        )
        rev_amounts.append(
            _amount_yi(
                float(cast("float", r["amount"])) if r.get("amount") is not None else None
            )
        )
    reversal = detect_trend_reversal(rev_closes, rev_opens, rev_highs, rev_lows, rev_amounts)
    if reversal["insufficient"]:
        missing.append("反转确认数据不足（完成 bar < 22）")

    # 仓位单一来源（2026-11-04）：序号来自 `_compose_card` 的唯一一次
    # derive_position_index（或继承的上游卡同源序号）。文案只由序号查表，禁止另行重算。
    position = card.evidence.position
    position_index = position.index if position is not None else None
    if position_index is not None:
        pos_text = "建议仓位：" + engine.POSITION_LADDER[position_index]
    else:
        # 仓位不可用（旧卡无 position_index / 无仓位）：只回放继承文案，不猜序号
        pos_text = card.inherited_position_text or ""
    # 账本与序号同源：都从 evidence 取，禁止本处另算（双真相源红线）
    ledger = card.evidence.position_ledger
    position_ledger = ledger.model_dump() if ledger is not None else None
    # 溯源文案同样从账本派生（硬约束：与 position_index / 观察点同一份账本）
    causality_text = render_causality(ledger).model_dump() if ledger is not None else None
    position_source: str | None
    if card.refresh_slot == "after_close" and card.evidence.stage_source != "inherited_prev_day":
        position_source = "after_close_base"
    elif card.evidence.stage_source == "inherited_prev_day":
        position_source = "inherited_prev_day"
    elif card.refresh_slot in {"morning", "midday"} \
            and card.evidence.stage_source == "inherited_basis":
        # 盘前事件修正是否真的改了仓位，由 `_compose_card` 判定（`event_adjusted`），
        # 本处只决定「未修正」时的归类；缺省（None）= 未经过 _compose_card 的旧调用。
        position_source = position_source_hint or "inherited_basis"
    else:
        position_source = None

    # phase_evidence.reason：主线结论 + 闸门/趋势结论（V10，≤72 字）
    if tech["insufficient"]:
        tech_part = "技术位数据不足"
    elif tech["index_breakdown"]:
        tech_part = "指数破位"
    elif tech["mainline_breakdown"]:
        tech_part = "主线板块破位"
    else:
        tech_part = "趋势未破位"
    mstate = mainline_facts.get("state")
    if mstate == "established":
        ml_strength = "强" if mainline_facts.get("strength") == "strong" else "弱"
        head_name = mainline_facts.get("name") or "未知"
        label = mainline_facts.get("mainline_label")
        if label and label != head_name:
            head_name = f"{label}·{head_name}"
        head = (
            f"主线：{head_name}（主线成立·{ml_strength}，"
            f"超额 +{mainline_facts.get('excess')}pct，"
            f"数据日 {mainline_facts.get('data_date') or ''}）"
        )
    elif mstate == "none":
        head = "主线：无清晰主线"
    else:
        head = "主线：数据不可用"
    reason = f"{head}｜{tech_part}"
    # §5.10.4：强主线 + 技术位结论需同时在句内，60 字会吃掉结论 → 放宽到 72
    if len(reason) > 72:
        reason = reason[:72]

    next_anchor = engine.build_next_event_anchor(win.events, card.target_date)
    event_high_hint = engine.build_event_hint(next_anchor)
    return {
        # DEPRECATED（2026-11-04 T4）：见上方 level_entry 处说明，仅兼容旧客户端，勿删勿置 null
        "score": score,
        # §5.6：phase 供前端情绪周期块（stage 原样透出，含 launch/rally）
        "phase": card.evidence.stage,
        "level": level,
        "position_band": {
            "text": pos_text,
        },
        # 仓位档位序号（0-4）与推导账本：日历上色 / 溯源区的唯一依据（因果输出 spec §4.3）
        "position_index": position_index,
        "position_ledger": position_ledger,
        # 仓位来源（与 stage_source 独立）：描述本卡档位从哪来，null = 无法归类
        "position_source": position_source,
        # 溯源主区文案（Python 侧确定性生成）：{lead, lines}；无账本 → null
        "causality_text": causality_text,
        "phase_evidence": {
            "reason": reason,
            "slope": None,
            "technical": tech,
            "reversal": reversal,
            # 判定路径（问题 1）：前端据此区分「沿用」与「判定」，避免中性兜底被当成强结论
            "stage_source": card.evidence.stage_source,
            # 盘中因子（spec §4.4）：仅 midday 有值；不可用时字段存在且为 null，
            # 使「数据不可用」本身也成为可统计样本（阈值离线寻优的自变量来源）
            "intraday_factors": (
                intraday_factors.model_dump() if intraday_factors is not None else None
            ),
        },
        "basis_data_date": _normalize_ymd(rows[-1].get("trade_date")) if rows else None,
        # C1：近 7 日情绪温度序列（情绪强度维度，与趋势档位分开表达）；无归档 → 空数组
        "temperature_series": temp_series,
        # T7（spec §3）：证据区温度因子一行。温度是 detect_stage 的 sentiment 因子来源，
        # 已从主视觉移除但保留在证据链中；只给数值与差值，**不引分档词**（冰点/低迷等
        # 归情绪温度，节奏侧不得再借用语汇）。无归档 → None（前端不渲染该行）。
        "temperature_note": _temperature_note(temp_series),
        # 展示通道：全量展示窗（需求 2，不限 5 交易日；display_events 为空=旧构造/
        # 分析子窗/旧测试替身场景 → 回退 events 保持契约稳定）。先过滤噪音/同日重复
        # 再投影，使 limit=30 在去噪音后才生效（spec §11-4）；过滤 high/medium +
        # 上限 30（G1 防 earnings 密集噪音 / 列表爆炸）；档位通道见上方 win_highs（仅 high）。
        "event_window": engine.project_event_window(
            _filter_event_window(
                list(getattr(win, "display_events", None) or win.events)
            ),
            importance_min="medium",
            limit=30,
        ),
        "event_source_missing": (
            win.source_missing or getattr(win, "calendar_uncovered", False)
        ),
        # 原点 = 目标交易日（该卡描述的那一天）；basis_date 已是证据日，不可用作原点
        "next_event_anchor": next_anchor,
        # 近窗口末日（裁决 C4）：前端按 event.date <= near_end_date 切分平铺/折叠；
        # 越年 → None（前端近窗全部平铺）。与 event_high_hint 同源数值 NEAR_HINT_DAYS。
        "event_window_near_end_date": _near_window_end_date(card.target_date),
        # 事件临近提示（与 next_event_anchor 同源，共用 skip 逻辑）
        "event_high_hint": event_high_hint,
        # 暂无冲突检测器（Phase 4 态 ↔ Stage 5 态不同源，见 spec §2.2）：恒 False。
        # 前端 conflict 为必填 bool，不可置 null；接入检测器前保持此常量。
        "conflict": False,
        "branches": branches,
        "data_missing": missing,
        # 跨日变更归因（spec §10）：默认关 → null；开关打开后由 _build_delta_payload 产出
        "delta": delta_payload,
        "delta_narrative": delta_narrative,
        # 下一张卡算 delta 的输入（必须落库，否则下一张只能比仓位）
        "mainline_snapshot": {
            "name": (mainline or {}).get("name"),
            "excess": (mainline or {}).get("excess"),
            "strength": (mainline or {}).get("strength"),
            "state": (mainline or {}).get("state"),
        },
    }


async def run(state: AgentState) -> dict[str, object]:
    """生成并落库节奏大师卡。

    state 为 scheduler 注入的 AgentState（含 refresh_slot/target_date）。
    """
    try:
        slot = str(state.get("refresh_slot") or "after_close")
        if slot not in REFRESH_SLOTS:
            slot = "after_close"
        basis = str(state.get("report_date") or shanghai_today().isoformat())
        target_override = state.get("target_date")
        if not (isinstance(target_override, str) and target_override):
            target_override = None
        card, rows, win, position_source, intraday_factors = await _compose_card(
            basis, slot, target_override
        )
        if not card.synthesis_available:
            logger.warning(
                "rhythm_master.degraded reason=%s slot=%s target_date=%s",
                DEGRADED_MODEL, card.refresh_slot, card.target_date,
            )
        # delta 必须先算完：它会往 card.evidence.data_missing 追加留痕，
        # 而 _build_rhythm_card 会把这份留痕带进 payload。
        delta_payload, narrative_text = await _build_delta_payload(
            card=card, slot=slot, basis_date=card.basis_date,
            mainline_facts=card.mainline_facts, rows=rows,
        )
        content = {
            "schema_version": "1.0",
            "target_date": card.target_date,
            "basis_date": card.basis_date,
            "refresh_slot": card.refresh_slot,
            "evidence": card.evidence.model_dump(),
            "synthesis": card.synthesis.model_dump() if card.synthesis else None,
            "synthesis_available": card.synthesis_available,
            "rhythm_card": _build_rhythm_card(
                card, win, rows, card.mainline_facts,
                position_source_hint=position_source,
                intraday_factors=intraday_factors,
                delta_payload=delta_payload,
                delta_narrative=narrative_text,
            ),
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
