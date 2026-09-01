"""预测能力执行服务 — 影响持续性推演。

独立可复用推理包：输入溯源结果 + 事实快照，输出 PredictionResult（含到期日）。
大盘溯源（review）内联调用；个股溯源/事件传导后续接入同一入口。
PR-A/T3：run_predict 返回状态化契约（ok/gate_skipped/llm_failed/parse_failed），
失败原因可区分，不再静默返回 None；另提供 predict_from_trace 独立执行入口
（缓存直读 → DB 重建 → trade_date 校验 → run_predict → 落库）。

P2 越年裁决（2026-08-14）：到期日不再因 chinese_calendar 覆盖（2004-2026）越年而整条
失败——改为逐档容错，越年档按「周末+已发布节假日(HOLIDAYS_EXTRA)」近似计算并显式标记
approximate（wire 键 due_dates_approximate），其余档精确。理由：验证器对照扫描日单日
符号（低信噪比），精确日历无统计增益；越年显式标注优于整条 skipped（预判功能停产）。
"""

import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Literal

import chinese_calendar  # type: ignore[import-untyped]  # 覆盖 2004-2026，与 utils/date.py 同源
import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.config import settings
from aistock_agent.prompts.workers.prediction import (
    PREDICTION_CHAT_PROMPT,
    PREDICTION_PROMPT,
)
from aistock_agent.schemas.market_trace import (
    DataReadiness,
    MarketTraceResult,
    MarketTraceSnapshot,
    PhenomenonDiscoveryResult,
    ReviewArtifact,
)
from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult
from aistock_agent.services.cache import get_cached_review
from aistock_agent.services.data_client import node_api
from aistock_agent.services.prediction_targets import classify_target
from aistock_agent.services.target_profile import make_target
from aistock_agent.services.llm import (
    get_deep_think,
    get_quick_think,
    with_chat_structured_output,
)
from aistock_agent.utils.date import add_trading_days

logger = structlog.get_logger()

# horizon → 到期交易日偏移（确定性计算，LLM 不输出日期）
HORIZON_TRADING_DAY_OFFSETS: dict[str, int] = {
    "short": 5,
    "mid": 20,
    "long": 120,
}

# skipped 落库默认文案（gate_skipped 的 reason 为空时兜底）
_DEFAULT_SKIP_REASON = "prediction skipped"

# 代码围栏剥离 — 防御 LLM 可能包裹的 ```json ... ```
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)

# LLM 常见多余键（PredictionResult extra="forbid" 下会导致校验失败）
_EXTRA_KEYS_TO_DROP = (
    "thinking",
    "analysis",
    "reasoning",
    "thoughts",
    "thought",
    "explanation",
)


class TraceUnavailableError(Exception):
    """溯源数据不可用（缓存与 DB 均无法重建 trace，或 snapshot trade_date 不匹配）。"""


@dataclass(frozen=True)
class PredictionRunResult:
    """预测执行结果：状态 + 预测工件 + 各档位到期交易日 + 原因 + 近似档标记。

    状态语义（S2 契约）：
    - ok：prediction + due_dates 完整产出（越年档降级为近似并记入 approximate_horizons）；
    - gate_skipped：attribution_status 门禁未过（prediction/due_dates 为空）；
    - llm_failed：LLM 调用异常（瞬时失败，可重试）；
    - parse_failed：载荷解析/校验失败（不可重试，属 LLM 输出质量问题）。
    """

    status: Literal["ok", "gate_skipped", "llm_failed", "parse_failed"]
    prediction: PredictionResult | None = None
    due_dates: dict[str, str] = field(default_factory=dict)  # {horizon: YYYY-MM-DD}
    approximate_horizons: list[str] = field(default_factory=list)  # 越年近似档位名列表
    reason: str = ""


def _strip_code_fences(text: str) -> str:
    m = _CODE_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


def _extract_first_json_object(text: str) -> dict[str, object] | None:
    """扫描文本提取第一个平衡的 JSON 对象；失败返回 None（兜底，主路径不依赖）。"""
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    parsed = json.loads(text[start : i + 1])
                    return parsed if isinstance(parsed, dict) else None
                except json.JSONDecodeError:
                    return None
    return None


def _coerce_prediction_payload(raw_text: str) -> dict[str, object]:
    """LLM 原始输出 → 可校验 dict：剥离围栏、提取 JSON、剔除多余键、兜底 schema_version。"""
    text = _strip_code_fences(raw_text).strip()
    data: object | None = None
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
    if data is None:
        data = _extract_first_json_object(text)
    if not isinstance(data, dict):
        raise ValueError("prediction output is not a JSON object")
    for key in _EXTRA_KEYS_TO_DROP:
        data.pop(key, None)
    # 双保险：防御 LLM 漏 schema_version（834ddf9 之外再兜底）；升 3.0 同步（Spec A §3.3）
    data.setdefault("schema_version", "3.0")
    return data


def _collect_allowed_evidence_ids(
    trace: MarketTraceResult, snapshot: MarketTraceSnapshot
) -> set[str]:
    """溯源结果中实际引用过的全部证据 ID + 快照已确认现象的 fact_ids（预测只能引用这些）。"""
    ids: set[str] = set()
    for candidate in trace.candidates:
        ids.update(candidate.supporting_evidence_ids)
        ids.update(candidate.counter_evidence_ids)
        if candidate.chain is not None:
            for node in candidate.chain.nodes:
                ids.update(node.evidence_ids)
    discovery = snapshot.phenomenon_discovery
    if discovery.primary is not None:
        ids.update(discovery.primary.fact_ids)
    for item in discovery.concurrent_phenomena:
        ids.update(item.fact_ids)
    return ids


def _build_prediction_input(
    trace: MarketTraceResult, snapshot: MarketTraceSnapshot
) -> dict[str, object]:
    """压缩溯源结果与快照关键字段为 LLM 输入包。"""
    chains: list[dict[str, object]] = []
    for candidate in trace.candidates:
        if candidate.chain is None:
            continue
        chains.append({
            "id": candidate.id,
            "category": candidate.category,
            "status": candidate.status,
            "verdict": candidate.verdict,
            "stages": [
                {"stage": node.stage, "claim": node.claim, "evidence_ids": node.evidence_ids}
                for node in candidate.chain.nodes
            ],
        })
    return {
        "attribution_status": trace.attribution_status,
        "primary_chain_id": trace.primary_chain_id,
        "alternative_chain_id": trace.alternative_chain_id,
        "confidence": trace.confidence,
        "unresolved_questions": trace.unresolved_questions,
        "chains": chains,
        "phenomenon_discovery": snapshot.phenomenon_discovery.model_dump(mode="json"),
        "a_share": {
            "indices": snapshot.a_share.get("indices"),
            "sectors": snapshot.a_share.get("sectors"),
        },
        "trade_date": snapshot.trade_date,
    }


def _compute_due_dates(
    trade_date: str, horizons: list[PredictionHorizon]
) -> tuple[dict[str, str], list[str]]:
    """确定性计算各档位到期交易日（逐档容错，P2 裁决）。

    对每档：add_trading_days 推进 N 个交易日（内部 is_trading_day 对越年走
    「周末+已发布节假日 HOLIDAYS_EXTRA」兜底，不抛）；随后用 chinese_calendar 覆盖探测
    该 due date：超出覆盖范围（2004-2026，如 2027 年，节假日数据 2026 年底才发布）
    → 该档到期日依赖未确认的节假日，显式标记为近似（approximate_horizons），不整条失败。

    Returns:
        (due_dates, approximate_horizons)：due_dates 每档 YYYY-MM-DD；
        approximate_horizons 为越年降级为近似的档位名列表（其余档精确）。
    """
    base = date.fromisoformat(trade_date)
    due_dates: dict[str, str] = {}
    approximate_horizons: list[str] = []
    for h in horizons:
        due = add_trading_days(base, HORIZON_TRADING_DAY_OFFSETS[h.horizon])
        due_str = due.isoformat()
        due_dates[h.horizon] = due_str
        # 覆盖探测：静默给出近似日期会误导到期验证对照，故显式标近似（G7 语义保留，
        # 由「整条失败」改为「标注降级」——P2 裁决：越年标注优于预测停产）
        try:
            chinese_calendar.is_workday(due)
        except (NotImplementedError, ValueError):
            approximate_horizons.append(h.horizon)
    return due_dates, approximate_horizons


# A3 确定性钳制：LLM 不产数值，confidence 由历史命中率后处理覆盖
_CONF_ORDER = {"high": 2, "medium": 1, "low": 0}


def _apply_confidence_cap(
    horizon: str,
    llm_conf: str,
    stats: dict[str, tuple[dict[str, object], dict[str, object]]] | None,
    *,
    mid_enabled: bool,
) -> tuple[str, str]:
    """确定性钳制 confidence（LLM 不产数值，此处为后处理覆盖）。

    - long 桶不启用；mid 桶仅 mid_enabled=True 时启用；short 桶恒启用。
    - stats 为 None 或 horizon 不在其中 → 不钳制（保 LLM 原值）。
    """
    if horizon == "long":
        return llm_conf, "llm"
    if horizon == "mid" and not mid_enabled:
        return llm_conf, "llm"
    if not stats or horizon not in stats:
        return llm_conf, "llm"
    from aistock_agent.services.prediction_stats import clamp_confidence_by_bucket

    hit_summary, baseline_summary = stats[horizon]
    cap, _ = clamp_confidence_by_bucket(horizon, hit_summary, baseline_summary)
    if cap and _CONF_ORDER.get(llm_conf, 0) > _CONF_ORDER.get(cap, 0):
        return cap, "deterministic"
    return llm_conf, "llm"


def _load_horizon_stats(
    records: list[dict[str, object]],
) -> dict[str, tuple[dict[str, object], dict[str, object]]]:
    """从 verified 记录按 horizon 聚合命中/基线统计。

    只取 entry.result ∈ {hit, miss}（剔除 insufficient 与 early_exit-only entry），
    逐档喂给 hit_rate_summary / baseline_neutral_summary（内部再按 methodology_version
    2.0 / 非 approximate 过滤，LLM 不产数值，统计为确定性计算）。
    """
    from aistock_agent.services.prediction_stats import (
        baseline_neutral_summary,
        hit_rate_summary,
    )

    by_horizon: dict[str, list[dict[str, object]]] = {}
    for rec in records:
        verification = rec.get("verification")
        if not isinstance(verification, dict):
            continue
        for horizon, entry in verification.items():
            if not isinstance(entry, dict):
                continue
            if entry.get("result") not in {"hit", "miss"}:
                continue
            by_horizon.setdefault(horizon, []).append(entry)

    stats: dict[str, tuple[dict[str, object], dict[str, object]]] = {}
    for horizon, entries in by_horizon.items():
        hit = hit_rate_summary(entries)
        baseline = baseline_neutral_summary(entries)
        stats[horizon] = (hit, baseline)
    return stats


# A2 独立源冲突检测：独立源=数据获取通道不同；claim/LLM 文本不计入
def _news_majority(news_dirs: list[int]) -> int | None:
    if not news_dirs:
        return None
    pos = sum(1 for d in news_dirs if d > 0)
    neg = sum(1 for d in news_dirs if d < 0)
    if pos == neg:
        return None
    return 1 if pos > neg else -1


def corroborate_evidence(
    *,
    quote_dir: int | None,
    flow_dir: int | None,
    news_dirs: list[int],
    calendar_dir: int | None = None,
    global_dir: int | None = None,
    direction: str,
) -> dict[str, object]:
    """独立源冲突检测（A2）。独立源=数据获取通道不同；claim/LLM 文本不计入。

    v1 仅 quote/flow/news 三通道有确定性取数；calendar/global 恒 None（保留签名供扩展）。
    """
    sources: list[tuple[str, int]] = []
    if quote_dir is not None:
        sources.append(("quote", quote_dir))
    if flow_dir is not None:
        sources.append(("flow", flow_dir))
    news_dir = _news_majority(news_dirs)
    if news_dir is not None:
        sources.append(("news", news_dir))
    if calendar_dir is not None:
        sources.append(("calendar", calendar_dir))
    if global_dir is not None:
        sources.append(("global", global_dir))

    n_price = sum(1 for k, _ in sources if k != "quote")
    if not sources:
        return {"independent_sources": 0, "non_price_sources": 0,
                "conflict": False, "verdict": "insufficient"}

    target = 1 if direction == "bullish" else -1 if direction == "bearish" else 0
    conflict = target != 0 and any(
        (v > 0) != (target > 0) for _, v in sources if v != 0
    )

    if conflict:
        return {"independent_sources": len(sources), "non_price_sources": n_price,
                "conflict": True, "verdict": "conflicted"}
    if len(sources) >= 2 and n_price >= 1:
        return {"independent_sources": len(sources), "non_price_sources": n_price,
                "conflict": False, "verdict": "corroborated"}
    return {"independent_sources": len(sources), "non_price_sources": n_price,
            "conflict": False, "verdict": "insufficient"}


def _direction_sign(value: float | None) -> int | None:
    """确定性方向提取（LLM 不产数值）：正→1，负→-1，零→0，缺失→None。"""
    if value is None:
        return None
    return 1 if value > 0 else -1 if value < 0 else 0


# Spec A §3.1/§4.1：anchor.direction 归一化兜底的关键词映射
_BULLISH_RE = re.compile(r"涨|升|站上|突破|看多|反弹|创新高|放量上攻")
_BEARISH_RE = re.compile(r"跌|破|回踩|回落|看空|走弱|破位|下探|缩量退守")


def infer_prediction_direction(condition: str, scenario: str) -> str:
    """anchor.direction 兜底：从 condition/scenario 文本确定性提取方向。

    复现 Spec A §4.1 与 A2 _direction_sign 同构思路：LLM 产结构，判定确定性算。
    命中看多词 → bullish；命中看空词 → bearish；两词皆命中或皆无 → neutral
    （text extraction 兜底，避免 model_validate 直接 parse_failed）。
    """
    text = f"{condition} {scenario}"
    bull = _BULLISH_RE.search(text) is not None
    bear = _BEARISH_RE.search(text) is not None
    if bull == bear:
        return "neutral"
    return "bullish" if bull else "bearish"


def _normalize_conditions_anchor_direction(
    result: PredictionResult,
) -> PredictionResult:
    """条件化 schema 后处理：LLM 缺失 anchor.direction 时按文本确定性兜底。

    PredidctorAnchor.direction 为 schema 必填；Pydantic 对缺失字段直接 raise → LLM
    单条 condition 未自挂 direction 会导致整份 parse_failed。此处先按缺失值（direction
    缺省成 neutral 重放）后逐条补强——前提是 schema 层 direction 允许先由缺省值进入，
    这里对已是默认 neutral 且文本明显看多/看空的条目重判。返回新对象，不改原对象。
    """
    if not result.conditions:
        return result
    new_conds: list[object] = []
    for c in result.conditions:
        cur = c.anchor.direction
        # 仅对"未明确表态"（仍带默认/neutral）的锚点做文本兜底，显式 bullish/bearish 不覆盖
        if cur == "neutral":
            inferred = infer_prediction_direction(c.condition, c.scenario)
            if inferred != "neutral":
                new_conds.append(c.model_copy(
                    deep=True,
                    update={"anchor": c.anchor.model_copy(update={"direction": inferred})},
                ))
                continue
        new_conds.append(c)
    if all(n is o for n, o in zip(new_conds, result.conditions)):
        return result
    return result.model_copy(update={"conditions": new_conds})


def _corroboration_inputs(
    snapshot: dict[str, object],
    news: list[dict[str, object]],
) -> dict[str, object]:
    """从 run_chat_prediction 输入提取确定性方向（对齐 raw 结构键）。缺失 → None（通道不参与计数）。

    独立源=数据获取通道不同：quote/flow/news 各取确定性符号，LLM 文本/claim 不计入。
    """
    quote = snapshot.get("quote") if isinstance(snapshot, dict) else None
    flow = snapshot.get("flow") if isinstance(snapshot, dict) else None
    quote_dir = _direction_sign(
        (quote or {}).get("change_pct") if isinstance(quote, dict) else None
    )
    flow_dir = _direction_sign(
        (flow or {}).get("net_amount") if isinstance(flow, dict) else None
    )
    news_dirs: list[int] = []
    for n in news:
        d = n.get("direction") if isinstance(n, dict) else None
        s = _direction_sign(d)
        if s is not None:
            news_dirs.append(s)
    return {"quote_dir": quote_dir, "flow_dir": flow_dir, "news_dirs": news_dirs}


async def run_predict(
    trace: MarketTraceResult,
    snapshot: MarketTraceSnapshot,
    *,
    replay_context: dict[str, object] | None = None,
) -> PredictionRunResult:
    """对已溯源的因果链推演影响持续性（状态化契约，S2）。

    门禁：attribution_status ∈ {confirmed, hypothesis} 才预测；
    其余返回 gate_skipped。失败不再静默返回 None，而是按原因分类返回状态：
    - LLM 调用异常 → llm_failed（瞬时失败，可重试）；
    - 载荷解析/校验失败 → parse_failed（LLM 输出质量问题）；
    - 到期日越年 → ok + approximate_horizons（逐档容错：越年档降级近似并标注，P2 裁决）；
    - 未预期异常（非上述已分类状态）→ logger.error + 重新抛出（不吞 bug，
      上层消费者/端点负责兜底）。

    replay_context（P4 迭代回放，仅回放态传）：{recorded_prediction, verification_feedback,
    target, replay}——并入 LLM 输入作「历史验证结果反馈」，供变体逻辑重出预判后
    与同一 verification entries 对比；target 用于按标的读取验证画像（替代默认上证指数）。
    """
    if trace.attribution_status not in {"confirmed", "hypothesis"}:
        logger.info("prediction_skip_by_attribution_status", status=trace.attribution_status)
        return PredictionRunResult(
            status="gate_skipped",
            reason=f"attribution_status={trace.attribution_status}",
        )
    try:
        # LLM 调用（含输入构造、ainvoke、raw 文本提取）— 瞬时失败分类
        try:
            prompt_input = _build_prediction_input(trace, snapshot)
            # P4 回放：历史验证结果作为反馈注入输入（recorded prediction +
            # verification entries），LLM 据此按变体逻辑重出预判。
            if replay_context:
                prompt_input = {**prompt_input, **replay_context}
            # Spec B §4.3：run_predict 绑定——大盘溯源（review）target 恒为市场指数
            # （prediction.horizons[].target 归一化到"上证指数"），画像并入输入做参考；
            # 解析/读取失败 → 原样返回不阻断产出（对齐 chat 绑定红线）。
            # 回放态（P4）：target 由 case.meta 锚定（可能为板块/个股），按标的读画像。
            replay_symbol: str | None = None
            if replay_context and isinstance(replay_context.get("target"), str):
                replay_symbol = str(replay_context["target"])
            prompt_input = await _enrich_predict_input_for_symbol(
                prompt_input, replay_symbol or _MARKET_PROFILE_SYMBOL
            )
            llm = get_deep_think()
            messages = [
                SystemMessage(content=PREDICTION_PROMPT),
                HumanMessage(content=json.dumps(prompt_input, ensure_ascii=False, indent=2)),
            ]
            ai_message = await llm.ainvoke(messages)
            raw_text = (
                ai_message.content
                if isinstance(ai_message.content, str)
                else str(ai_message.content)
            )
        except Exception as exc:
            logger.warning("prediction.llm_failed", error=str(exc), exc_info=True)
            return PredictionRunResult(status="llm_failed", reason=str(exc))
        # 载荷解析/校验 — LLM 输出质量问题分类
        try:
            prediction = PredictionResult.model_validate(_coerce_prediction_payload(raw_text))
        except Exception as exc:
            logger.warning("prediction.parse_failed", error=str(exc), exc_info=True)
            return PredictionRunResult(status="parse_failed", reason=str(exc))
        # Spec A §4.1：anchor.direction 归一化兜底（load 后、使用前）——
        # LLM 未产 direction 时按 condition/scenario 文本确定性提取，避免 parse_failed。
        prediction = _normalize_conditions_anchor_direction(prediction)
        allowed = _collect_allowed_evidence_ids(trace, snapshot)
        # P1-1：证据 ID 过滤而非一票否决（对齐 run_chat_prediction）——单一幻觉不丢整体
        filtered = [sid for sid in prediction.evidence_ids if sid in allowed]
        if len(filtered) != len(prediction.evidence_ids):
            logger.warning(
                "prediction.evidence_filtered",
                dropped=len(prediction.evidence_ids) - len(filtered),
            )
        prediction = prediction.model_copy(update={"evidence_ids": filtered})
        # A3 确定性钳制：confidence 后处理覆盖（LLM 不产数值；拉取失败不钳制）
        try:
            records = await node_api.list_verified_predictions(limit=500)
        except Exception:
            records = None
        stats = _load_horizon_stats(records or [])
        for h in prediction.horizons:
            conf, source = _apply_confidence_cap(
                h.horizon,
                h.confidence,
                stats,
                mid_enabled=settings.prediction_conf_cap_mid != "high",
            )
            h.confidence = conf
            h.confidence_source = source
        # 到期日计算（逐档容错，P2）：越年档标近似（approximate_horizons），不整条失败
        due_dates, approximate_horizons = _compute_due_dates(
            snapshot.trade_date, prediction.horizons
        )
        return PredictionRunResult(
            status="ok",
            prediction=prediction,
            due_dates=due_dates,
            approximate_horizons=approximate_horizons,
        )
    except Exception as exc:
        # 未预期异常：不静默，重新抛出交由上层消费者/端点兜底
        logger.error("prediction.run_unexpected_failure", error=str(exc), exc_info=True)
        raise


async def _load_trace_and_snapshot(
    trace_id: str, trade_date: str
) -> tuple[MarketTraceResult, MarketTraceSnapshot]:
    """按读取链重建 (trace, snapshot)：a. 缓存直读 → b. DB 重建；都失败抛 TraceUnavailableError。"""
    # a. 缓存直读：ReviewArtifact dict → model_validate（旧缓存缺字段/多余字段按不可重建处理）
    cached = await get_cached_review(trade_date)
    if cached is not None:
        try:
            artifact = ReviewArtifact.model_validate(cached)
            logger.debug(
                "predict_from_trace.cache_hit",
                trace_id=trace_id,
                trade_date=trade_date,
                snapshot_id=artifact.snapshot.snapshot_id,
            )
            return artifact.trace, artifact.snapshot
        except Exception as exc:
            logger.warning("predict_from_trace.cache_invalid", error=str(exc))
    # b. DB 重建：analysis_reports.content.market_trace = {"snapshot": ..., "trace": ...}
    report = await node_api.get_analysis_report("review", trade_date)
    if report is not None:
        content = report.get("content")
        market_trace = content.get("market_trace") if isinstance(content, dict) else None
        if isinstance(market_trace, dict):
            snapshot_data = market_trace.get("snapshot")
            trace_data = market_trace.get("trace")
            if isinstance(snapshot_data, dict) and isinstance(trace_data, dict):
                try:
                    snapshot = MarketTraceSnapshot.model_validate(snapshot_data)
                    trace = MarketTraceResult.model_validate(trace_data)
                    logger.debug(
                        "predict_from_trace.db_rebuild",
                        trace_id=trace_id,
                        trade_date=trade_date,
                        snapshot_id=snapshot.snapshot_id,
                    )
                    return trace, snapshot
                except Exception as exc:
                    # extra="forbid" 下旧数据字段缺失/多余 → 按不可重建处理
                    logger.warning("predict_from_trace.db_rebuild_failed", error=str(exc))
    raise TraceUnavailableError(f"no trace available for review:{trade_date}")


def _replay_minimal_trace_snapshot(
    trade_date: str,
) -> tuple[MarketTraceResult, MarketTraceSnapshot]:
    """回放态最小合法 trace/snapshot（P4，无 DB 访问）。

    prediction 产片源（prediction_verified_scan）不落市场快照/溯源链，case.meta
    只带 {target, trade_date, prediction, verification}；真实上下文经 replay_context
    注入 run_predict 的 prompt_input（历史快照源 Spec D 未落地前，回放输入 =
    验证画像增强 + 原 prediction 输入，对齐计划 P4 裁决）。此处仅构造能通过
    run_predict 门禁/输入构造的最小结构对象（chains 空、a_share 空）。
    """
    discovery = PhenomenonDiscoveryResult(
        status="insufficient_data",
        primary=None,
        concurrent_phenomena=[],
        data_readiness=DataReadiness(
            market_data="incomplete",
            attribution_inputs="missing",
            causal_evidence="not_ready",
        ),
        diagnostics=[],
    )
    snapshot = MarketTraceSnapshot(
        snapshot_id=f"replay_{trade_date}",
        trade_date=trade_date,
        captured_at=datetime.now(UTC),
        a_share={},
        sources={},
        missing_fields=[],
        phenomenon_discovery=discovery,
    )
    trace = MarketTraceResult(
        schema_version="1.1",
        attribution_status="confirmed",
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="medium",
        unresolved_questions=[],
    )
    return trace, snapshot


async def _replay_predict_from_case(
    case_id: str, trade_date: str
) -> PredictionRunResult:
    """回放态预测：从 case slice meta 重建输入（无 DB 访问），并入验证反馈。

    REPLAY_CASE_ID 存在时由 predict_from_trace 顶部转调（P4）。从 case.meta 提取
    {target, trade_date, prediction, verification}：
    - recorded prediction + verification entries 作为回放上下文注入 run_predict
      ——LLM 看到「前次预判 + 到期验证结果」，据此按变体逻辑重出预判；
    - 验证画像按 case.meta target 读取（Target 维度一致，替代默认上证指数）。
    返回 PredictionRunResult；落库由调用方跳过（回放只读，post 已被 no-op）。
    """
    from aistock_agent.iterate.case_builder import load_case

    case = load_case(case_id)
    meta = case.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    meta_trade_date = str(meta.get("trade_date") or "")
    if meta_trade_date and meta_trade_date != trade_date:
        raise TraceUnavailableError(
            f"replay case meta trade_date {meta_trade_date} != trade_date {trade_date}"
        )
    trace, snapshot = _replay_minimal_trace_snapshot(meta_trade_date or trade_date)
    prediction = meta.get("prediction")
    prediction = prediction if isinstance(prediction, dict) else {}
    verification = meta.get("verification")
    verification = verification if isinstance(verification, dict) else {}
    target = str(meta.get("target") or "")
    replay_context: dict[str, object] = {
        "replay": True,
        "recorded_prediction": prediction,
        "verification_feedback": [
            v for v in verification.values() if isinstance(v, dict)
        ],
    }
    if target:
        replay_context["target"] = target
    return await run_predict(trace, snapshot, replay_context=replay_context)


async def predict_from_trace(
    trace_id: str, trade_date: str
) -> tuple[PredictionRunResult, dict[str, object] | None]:
    """大盘溯源后接预测的独立执行入口（PR-A/T3）。

    流程：缓存直读 → DB 重建 → snapshot.trade_date 校验 → run_predict → 按状态落库。
    trace_id 当前用于日志标识（source_id 统一为 review:{trade_date}，与 review 内联落库一致）；
    后续个股溯源/事件传导复用本入口时，trace_id 可作为溯源标识扩展点。

    REPLAY 回放态（P4）：REPLAY_CASE_ID 环境变量存在时，顶部转调
    `_replay_predict_from_case`——从 case slice meta 重建输入（无 DB 访问）、
    并入验证反馈，且不落库（record=None）。

    落库规则：
    - ok → 完整 prediction 记录（status 不传，Node 默认 pending）；越年近似档
      经 due_dates_approximate 显式标记（Node 侧合并进 prediction jsonb）；
    - gate_skipped → status=skipped + skip_reason（硬约束 3 闭环）；
    - llm_failed / parse_failed → 瞬时失败不落库（由调用方决定重试），record=None。

    Returns:
        (run_result, save_prediction 返回的 record 或 None)。
    """
    if os.environ.get("REPLAY_CASE_ID"):
        # P4 回放态：从 case slice meta 重建输入，回放只读不落库
        return (
            await _replay_predict_from_case(os.environ["REPLAY_CASE_ID"], trade_date),
            None,
        )
    trace, snapshot = await _load_trace_and_snapshot(trace_id, trade_date)
    # 校验：快照日期必须与目标交易日一致（对照 review.py L983 先例，防旧快照误用）
    if snapshot.trade_date != trade_date:
        raise TraceUnavailableError(
            f"snapshot trade_date {snapshot.trade_date} != trade_date {trade_date}"
        )
    result = await run_predict(trace, snapshot)
    if result.status == "ok":
        assert result.prediction is not None
        payload: dict[str, object] = {
            "source_type": "market_trace",
            "source_id": f"review:{trade_date}",
            "schema_version": result.prediction.schema_version,
            "prediction": result.prediction.model_dump(mode="json"),
            "due_dates": result.due_dates,
        }
        if result.approximate_horizons:
            # 越年近似档显式标记（仅非空携带，向后兼容；Node 侧合并进 prediction jsonb）
            payload["due_dates_approximate"] = result.approximate_horizons
    elif result.status == "gate_skipped":
        # 硬约束 3：skipped 落库闭环（skip_reason 存 prediction 对象内）
        payload = {
            "source_type": "market_trace",
            "source_id": f"review:{trade_date}",
            "schema_version": "1.0",
            "status": "skipped",
            "prediction": {"skip_reason": result.reason or _DEFAULT_SKIP_REASON},
            "due_dates": {},
        }
    else:
        # llm_failed / parse_failed：瞬时失败，不落库
        return result, None
    try:
        record = await node_api.save_prediction(payload)
    except Exception as exc:
        logger.error(
            "prediction.persist_failed",
            source_id=f"review:{trade_date}",
            error=str(exc),
            exc_info=True,
        )
        raise
    return result, record


async def save_skipped_prediction(source_id: str, reason: str) -> dict[str, object] | None:
    """落一条 skipped 预测记录（供消费者/端点在 TraceUnavailableError 等场景调用）。

    内部调 save_prediction：status=skipped、prediction={"skip_reason": reason}、
    due_dates={}、schema_version=1.0。
    """
    payload: dict[str, object] = {
        "source_type": "market_trace",
        "source_id": source_id,
        "schema_version": "1.0",
        "status": "skipped",
        "prediction": {"skip_reason": reason},
        "due_dates": {},
    }
    return await node_api.save_prediction(payload)


def _gate_chat_snapshot(snapshot: dict) -> str | None:
    """对话内预测门禁：缺行情关键字段返回原因（None = 通过）。

    - quote 必须为非空 dict（行情关键字段缺失 → synth 走既有 D35 降级提示）；
    - flow 不设门禁条件：缺失/为空 dict 均通过（指数无个股资金流，
      属"不适用"而非"缺失"；flow_id 派生逻辑同样以"非空 dict"为准）；
    - trade_date 必须可解析（到期日确定性计算的基准日）。
    """
    quote = snapshot.get("quote")
    if not isinstance(quote, dict) or not quote:
        return "missing quote"
    trade_date = snapshot.get("trade_date")
    if not isinstance(trade_date, str):
        return "missing trade_date"
    try:
        date.fromisoformat(trade_date)
    except ValueError:
        return "invalid trade_date"
    return None


def _chat_item_ids(snapshot: dict, news: list[dict]) -> tuple[str, str | None, list[str | None]]:
    """现状快照各输入项的 evidence_id（LLM 输入与后处理过滤共用同一套 id）。

    - quote：优先取快照自带 quote_evidence_id，缺省 "quote:{symbol}"（确定性）；
    - flow：仅在快照携带非空 dict flow 时派生 flow_id，否则为 None
      （指数无个股资金流 → LLM 输入不含 capital_flow 块，也不可被预测引用）；
    - news：逐项取 evidence_id/id/source_id 首个非空字符串，无 id 项为 None
      （不可被预测引用，LLM 输入中也不携带 evidence_id）。
    """
    symbol = str(snapshot.get("symbol", ""))
    quote_id = snapshot.get("quote_evidence_id") or f"quote:{symbol}"
    flow = snapshot.get("flow")
    if isinstance(flow, dict) and flow:
        flow_id: str | None = snapshot.get("flow_evidence_id") or f"flow:{symbol}"
    else:
        flow_id = None
    news_ids: list[str | None] = []
    for item in news:
        nid = item.get("evidence_id") or item.get("id") or item.get("source_id")
        news_ids.append(nid if isinstance(nid, str) and nid else None)
    return quote_id, flow_id, news_ids


def _collect_chat_evidence_ids(snapshot: dict, news: list[dict]) -> set[str]:
    """输入快照/新闻中实际存在项的 evidence_id 集合（预测只能引用这些）。"""
    quote_id, flow_id, news_ids = _chat_item_ids(snapshot, news)
    ids = {quote_id}
    if flow_id is not None:
        ids.add(flow_id)
    ids.update(nid for nid in news_ids if nid is not None)
    return ids


def _build_chat_prediction_input(
    snapshot: dict, news: list[dict], context: dict
) -> dict[str, object]:
    """现状快照驱动的 LLM 输入包（行情/资金/新闻 + 上下文，无溯源因果链）。

    指数场景（flow 缺失）→ 不携带 capital_flow 块（"不适用"而非"缺失"，不编造指数资金流）。
    """
    quote_id, flow_id, news_ids = _chat_item_ids(snapshot, news)
    news_blocks: list[dict[str, object]] = []
    for item, nid in zip(news, news_ids):
        block = dict(item)
        if nid is not None:
            block["evidence_id"] = nid
        news_blocks.append(block)
    payload: dict[str, object] = {
        "input_mode": "snapshot_driven",
        "symbol": snapshot.get("symbol", ""),
        "trade_date": snapshot.get("trade_date"),
        "context": context,
        "quote": {"evidence_id": quote_id, "data": snapshot.get("quote")},
        "news": news_blocks,
    }
    if flow_id is not None:
        payload["capital_flow"] = {"evidence_id": flow_id, "data": snapshot.get("flow")}
    return payload


async def _enrich_chat_input_with_profile(
    prompt_input: dict[str, object], symbol: str
) -> dict[str, object]:
    """Spec B §4.3：预判产出方绑定——产出前读 target 验证画像并入 LLM 输入。

    依赖 skill 的 read_validation_profile + enrich_prediction_input（缓存优先，miss 拉取重算）。
    红线：画像只作输入参考；解析 target 失败/读取异常 → 原样返回（不阻断产出）。
    """
    from aistock_agent.skills.prediction_validation import (
        enrich_prediction_input,
        read_validation_profile,
    )

    if not symbol:
        return prompt_input
    target = make_target(symbol)
    if target is None:
        logger.debug("chat_prediction.enrich_skipped_no_target", symbol=symbol)
        return prompt_input
    try:
        profile = await read_validation_profile(target)
    except Exception:  # noqa: BLE001
        logger.debug("chat_prediction.enrich_profile_failed", symbol=symbol, exc_info=True)
        return prompt_input
    return enrich_prediction_input(prompt_input, profile)


# run_predict（大盘溯源）反哺画像的代表 target：run_predict 预测的是市场整体影响
# 持续性，prediction.horizons[].target 归一化到上证指数（INDEX_TARGETS 首个）。绑定
# 到该指数画像即可复用验证闭环；target 解析/读取失败 → 原样返回（同 chat 红线）。
_MARKET_PROFILE_SYMBOL = "上证指数"


async def _enrich_predict_input_for_symbol(
    prompt_input: dict[str, object], symbol: str
) -> dict[str, object]:
    """Spec B §4.3：按 symbol 读验证画像并入预判输入（回放态按 case.meta target 分层）。

    大盘溯源（review）固定上证指数；P4 迭代回放用产片源锚定的 target（可能为
    板块/个股），Target 维度一致。红线：画像只作输入参考；解析 target 失败/读取
    异常 → 原样返回（不阻断产出）。
    """
    from aistock_agent.skills.prediction_validation import (
        enrich_prediction_input,
        read_validation_profile,
    )

    target = make_target(symbol)
    if target is None:
        return prompt_input
    try:
        profile = await read_validation_profile(target)
    except Exception:  # noqa: BLE001
        logger.debug("predict_input.enrich_profile_failed", symbol=symbol, exc_info=True)
        return prompt_input
    return enrich_prediction_input(prompt_input, profile)


async def _enrich_market_predict_input(prompt_input: dict[str, object]) -> dict[str, object]:
    """Spec B §4.3：run_predict（大盘溯源）预判产出方绑定——读市场指数验证画像并入输入。"""
    return await _enrich_predict_input_for_symbol(prompt_input, _MARKET_PROFILE_SYMBOL)


# P0-3：chat 禁点位红线后处理硬校验（防 prompt 改动静默失效；仅 chat 入口）
_PRICE_PATTERN = re.compile(r"\d+(?:\.\d{1,2})?\s*元")
_RANGE_PATTERN = re.compile(r"\d{3,6}\s*[-~至]\s*\d{3,6}\s*(?:点|区间)?")
# D5 补丁：裸数字点位——上下文词（指数/点位/大盘）+ 3-6 位数字（可带"点"后缀），
# 拦截"目标点位 12000"/"上证指数 12000"式绕过；刻意不匹配"成交额达 1000 亿"（量词非点位）
_POINT_PATTERN = re.compile(
    r"(?:上证|深证|创业板|科创|沪深|大盘|指数|点位|目标点位)\s*\d{3,6}(?:\s*点)?")
_ABSOLUTE_VERBS = ("维持", "涨至", "跌至", "看至", "目标价")  # D5：不加入"达"（防误杀量词）
_REDACTED_TEXT = "（点位表述已按合规要求移除）"


def _contains_absolute_point(text: str) -> bool:
    """绝对点位检测：价格（X 元）/ 点位区间（3500-3600 点）/ 绝对动词 / 裸数字点位（D5）。

    刻意不匹配"涨幅/涨跌幅 20%"（相对描述）与"围绕当前价位窄幅整理"（相对区间）——
    产品红线禁的是绝对价格/点位（PREDICTION_CHAT_PROMPT 语义）。"""
    if not text:
        return False
    if _PRICE_PATTERN.search(text):
        return True
    if _RANGE_PATTERN.search(text):
        return True
    if _POINT_PATTERN.search(text):
        return True
    return any(v in text for v in _ABSOLUTE_VERBS)


_HARD_VALIDATED_FIELDS = ("metric_projection", "evolution_narrative", "attribution_summary")


def _hard_validate_chat_prediction(prediction: PredictionResult, symbol: str) -> PredictionResult:
    """chat 预测后处理红线硬校验：命中绝对点位 → 剥离该字段并记独立日志（G5：不静默）。

    覆盖全文本字段（A6：narrative 是绕行通道）：顶层 evolution_narrative/attribution_summary
    以及每个 horizon 的 metric_projection。命中 → 用占位文案替换，绝不静默丢弃。"""
    changes: dict[str, object] = {}
    for fld in ("evolution_narrative", "attribution_summary"):
        val = getattr(prediction, fld, None)
        if isinstance(val, str) and _contains_absolute_point(val):
            logger.warning(
                "chat_prediction.hard_validation_failed",
                field=fld,
                symbol=symbol,
            )
            changes[fld] = _REDACTED_TEXT
    # horizon 级 metric_projection（prediction_jsonb 全文本覆盖，A6）
    new_horizons: list[object] = []
    for h in prediction.horizons:
        mp = getattr(h, "metric_projection", None)
        if isinstance(mp, str) and _contains_absolute_point(mp):
            logger.warning(
                "chat_prediction.hard_validation_failed",
                field="metric_projection",
                symbol=symbol,
            )
            new_horizons.append(h.model_copy(update={"metric_projection": _REDACTED_TEXT}))
        else:
            new_horizons.append(h)
    horizons_changed = (
        len(new_horizons) != len(prediction.horizons)
        or any(n is not o for n, o in zip(new_horizons, prediction.horizons))
    )
    if changes or horizons_changed:
        update = dict(changes)
        if horizons_changed:
            update["horizons"] = new_horizons
        prediction = prediction.model_copy(update=update)
    return prediction


async def _persist_chat_prediction(
    prediction: PredictionResult,
    snapshot: dict,
    due_dates: dict[str, str],
    approximate_horizons: list[str],
) -> dict[str, object] | None:
    """方案A：对话预判落库（Spec A §4.2/§11），target 分流防 pending 堆积。

    classify_target(horizons[0].target)：index/sector → status=pending 纳入 16:00
    到期验证；stock → status=skipped + skip_reason（v1 验证器个股源未接，§9-5），
    避免无法验证的个股记录污染验证队列。落库失败不阻断返回值（"永不 500"）。
    """
    source_id = f"chat:{snapshot.get('symbol', '')}:{snapshot.get('trade_date', '')}"
    payload: dict[str, object] = {
        "source_type": "chat_prediction",
        "source_id": source_id,
        "schema_version": prediction.schema_version,
        "prediction": prediction.model_dump(mode="json"),
        "due_dates": due_dates,
    }
    if approximate_horizons:
        payload["due_dates_approximate"] = approximate_horizons
    try:
        tgt = str(prediction.horizons[0].target or "")
        if classify_target(tgt) == "stock":
            # 个股 target 超出 v1 验证范围 → skipped（完整预判仍保留便于追溯）
            payload["status"] = "skipped"
            payload["prediction"]["skip_reason"] = "chat 个股 target 超出 v1 验证范围"
        return await node_api.save_prediction(payload)
    except Exception as exc:
        logger.warning("chat_prediction.persist_failed", source_id=source_id, error=str(exc))
        return None


async def run_chat_prediction(
    snapshot: dict, news: list[dict], context: dict
) -> PredictionResult | None:
    """无溯源链的现状快照驱动预测入口（CHAT QA 对话内预测专用，Phase 4-1）。

    输入契约（Task 2+ 由 skill_executor 汇总 Evidence.raw 构造）：
    - snapshot：{"symbol", "trade_date", "quote"?, "flow"?,
      "quote_evidence_id"?, "flow_evidence_id"?}——quote/flow 分别对齐
      stock_snapshot raw.quote / capital_flow raw.flow 结构；flow 为可选
      （指数无个股资金流属"不适用"而非"缺失"）；
    - news：list[dict]，每项可选 evidence_id/id/source_id（无 id 项不可被引用）；
    - context：用户问题上下文（question/time_range 等），透传 LLM 参考。

    门禁：快照缺行情关键字段（quote 非空 dict、trade_date 可解析）→
    返回 None，由 synth_answer 走既有 D35 降级提示；flow 缺失/为空 dict 均
    通过门禁（指数场景，LLM 输入不携带 capital_flow 块，不编造指数资金流）。后处理：
    - prediction_status 强制 "hypothesis"（无溯源链不得 confirmed）；
    - evidence_ids 只保留输入快照/新闻存在项（过滤而非抛错，区别于 run_predict）；
    - 落库（Spec A §4.2/§9-2 方案 A）：恢复 _compute_due_dates 到期日计算，经
      _persist_chat_prediction 写入 prediction_records——index/sector 纳入 16:00
      到期验证，stock 分流 skipped（v1 验证器个股源未接，防 pending 堆积）。
    任一失败返回 None（"永不 500"铁律）。不产生交易指令。
    """
    gate_reason = _gate_chat_snapshot(snapshot)
    if gate_reason is not None:
        logger.info("chat_prediction.gate_skip", reason=gate_reason)
        return None
    try:
        prompt_input = _build_chat_prediction_input(snapshot, news, context)
        # Spec B §4.3：验证画像并入 LLM 输入（预判反哺；only 输入参考，异常/无 target 不阻断）
        prompt_input = await _enrich_chat_input_with_profile(
            prompt_input, str(snapshot.get("symbol", "") or ""),
        )
        # P10 计费口径：对齐 skill_executor 其它 skill，用 quick_think 单次调用
        # （deep_think 26-47s/次，chat UX 不可接受）；json_mode 结构化输出
        # （DeepSeek thinking 兼容，项目记忆 lesson 8）直接产出已解析的
        # PredictionResult，省去手动 raw 文本 + _strip_code_fences + validate。
        llm = get_quick_think()
        messages = [
            SystemMessage(content=PREDICTION_CHAT_PROMPT),
            HumanMessage(content=json.dumps(prompt_input, ensure_ascii=False, indent=2)),
        ]
        structured = with_chat_structured_output(llm, PredictionResult)
        prediction = await structured.ainvoke(messages)
        # Spec A §4.1：anchor.direction 归一化兜底（json_mode 结构化输出同样适用，
        # direction 缺省 neutral，LLM 不产时按文本确定性提取，不 parse_failed）
        prediction = _normalize_conditions_anchor_direction(prediction)
        allowed = _collect_chat_evidence_ids(snapshot, news)
        prediction = prediction.model_copy(
            update={
                "prediction_status": "hypothesis",
                "evidence_ids": [sid for sid in prediction.evidence_ids if sid in allowed],
            }
        )
        # P0-3：红线硬校验（chat 专属；run_predict 允许点位区间，不做此校验）
        prediction = _hard_validate_chat_prediction(
            prediction, str(snapshot.get("symbol", "")))
        # A3 确定性钳制：confidence 后处理覆盖（与 run_predict 同一后处理；拉取失败不钳制）
        try:
            records = await node_api.list_verified_predictions(limit=500)
        except Exception:
            records = None
        stats = _load_horizon_stats(records or [])
        for h in prediction.horizons:
            conf, source = _apply_confidence_cap(
                h.horizon,
                h.confidence,
                stats,
                mid_enabled=settings.prediction_conf_cap_mid != "high",
            )
            h.confidence = conf
            h.confidence_source = source
        # Spec A §4.2/§9-2（方案 A）：恢复到期日计算并落库。chat 预判 index/sector
        # 纳入 16:00 到期验证；stock 由 _persist_chat_prediction 分流 skipped。越年
        # 近似档经 due_dates_approximate 显式标记。
        due_dates, approximate_horizons = _compute_due_dates(
            str(snapshot["trade_date"]), prediction.horizons,
        )
        await _persist_chat_prediction(
            prediction, snapshot, due_dates, approximate_horizons,
        )
        # A2 独立源冲突检测接线：确定性方向提取自输入（LLM 不产数值）；结果仅作为
        # 独立证据字段，绝不覆盖 confidence（佐证信号 ≠ 置信度）。run_predict 路径
        # 无此接线——输入仅指数行情方向，恒 insufficient，不误报。
        inputs = _corroboration_inputs(snapshot, news)
        prediction.evidence_corroboration = corroborate_evidence(
            direction=prediction.horizons[0].direction, **inputs,
        )
        return prediction
    except Exception as exc:
        logger.warning("chat_prediction.failed", error=str(exc), exc_info=True)
        return None


def render_prediction_markdown(prediction: PredictionResult) -> str:
    """从已验证的 PredictionResult 渲染展示层 Markdown（可复用于各 agent）。"""
    status_map = {"confirmed": "已确认", "hypothesis": "假设推演", "insufficient": "证据不足"}
    phase_map = {
        "building": "影响形成",
        "peaking": "影响高峰",
        "decaying": "影响衰减",
        "returning": "回归常态",
    }
    horizon_label = {"short": "短线(1-5交易日)", "mid": "中线(1-4周)", "long": "长线(1-6月)"}
    lines: list[str] = []
    lines.append("## 影响持续性预判")
    lines.append(
        f"- 预测状态：{status_map.get(prediction.prediction_status, prediction.prediction_status)}"
    )
    if prediction.attribution_summary:
        lines.append(f"- 结论：{prediction.attribution_summary}")
    for h in prediction.horizons:
        label = horizon_label.get(h.horizon, h.horizon)
        lines.append(
            f"- **{label}**：{h.remaining_estimate}｜阶段：{phase_map.get(h.phase, h.phase)}"
            f"｜方向：{h.direction}｜置信：{h.confidence}"
        )
        lines.append(f"  - 验证对象：{h.target}｜预期：{h.metric_projection}")
    if prediction.evolution_steps:
        lines.append("- 演化路径：")
        for step in prediction.evolution_steps:
            lines.append(f"  - {step.label}：{step.text}")
    elif prediction.evolution_narrative:
        lines.append(f"- 演化路径：{prediction.evolution_narrative}")
    if prediction.risks:
        lines.append("- 风险因素：")
        for risk in prediction.risks:
            lines.append(f"  - {risk.factor}：{risk.invalidation}")
    lines.append("")
    return "\n".join(lines)
