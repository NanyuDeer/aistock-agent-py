"""预测能力执行服务 — 影响持续性推演。

独立可复用推理包：输入溯源结果 + 事实快照，输出 PredictionResult（含到期日）。
大盘溯源（review）内联调用；个股溯源/事件传导后续接入同一入口。
PR-A/T3：run_predict 返回状态化契约（ok/gate_skipped/llm_failed/parse_failed/due_dates_failed），
失败原因可区分，不再静默返回 None；另提供 predict_from_trace 独立执行入口
（缓存直读 → DB 重建 → trade_date 校验 → run_predict → 落库）。
"""

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import chinese_calendar  # type: ignore[import-untyped]  # 覆盖 2004-2026，与 utils/date.py 同源
import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.prediction import (
    PREDICTION_CHAT_PROMPT,
    PREDICTION_PROMPT,
)
from aistock_agent.schemas.market_trace import (
    MarketTraceResult,
    MarketTraceSnapshot,
    ReviewArtifact,
)
from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult
from aistock_agent.services.cache import get_cached_review
from aistock_agent.services.data_client import node_api
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

# skipped 落库默认文案（gate_skipped/due_dates_failed 的 reason 为空时兜底）
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


class DueDatesComputationError(Exception):
    """到期日计算失败（任一 horizon 计算异常或 due date 超出 chinese_calendar 覆盖范围）。"""


class TraceUnavailableError(Exception):
    """溯源数据不可用（缓存与 DB 均无法重建 trace，或 snapshot trade_date 不匹配）。"""


@dataclass(frozen=True)
class PredictionRunResult:
    """预测执行结果：状态 + 预测工件 + 各档位到期交易日 + 原因。

    状态语义（S2 契约）：
    - ok：prediction + due_dates 完整产出；
    - gate_skipped：attribution_status 门禁未过（prediction/due_dates 为空）；
    - llm_failed：LLM 调用异常（瞬时失败，可重试）；
    - parse_failed：载荷解析/校验失败（不可重试，属 LLM 输出质量问题）；
    - due_dates_failed：到期日计算失败（G7：不再静默降级 {}，显式标记）。
    """

    status: Literal["ok", "gate_skipped", "llm_failed", "parse_failed", "due_dates_failed"]
    prediction: PredictionResult | None = None
    due_dates: dict[str, str] = field(default_factory=dict)  # {horizon: YYYY-MM-DD}
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
    # 双保险：防御 LLM 漏 schema_version（834ddf9 之外再兜底）
    data.setdefault("schema_version", "1.0")
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


def _compute_due_dates(trade_date: str, horizons: list[PredictionHorizon]) -> dict[str, str]:
    """确定性计算各档位到期交易日；任一 horizon 计算异常或超日历覆盖 → 显式失败。

    覆盖守卫（G7 修复）：chinese_calendar 覆盖 2004-2026。due date 落在覆盖范围外
    （如 2027 年，节假日数据 2026 年底才发布）说明该日期依赖未确认的节假日——
    静默给出近似日期会误导到期验证对照，故显式抛 DueDatesComputationError，
    由 run_predict 落 due_dates_failed。库升级后 is_workday 不再抛异常，自动恢复精确。
    注：PR-B（holidays_extra 补充源）合入后，此处可改为 consult 补充源
    （post-merge 跟进项，本期不做）。
    """
    base = date.fromisoformat(trade_date)
    due_dates: dict[str, str] = {}
    for h in horizons:
        try:
            due_dates[h.horizon] = add_trading_days(
                base, HORIZON_TRADING_DAY_OFFSETS[h.horizon]
            ).isoformat()
        except Exception as exc:
            raise DueDatesComputationError(
                f"due date computation failed for horizon {h.horizon}: {exc}"
            ) from exc
    # 覆盖守卫：任一 due date 超出 chinese_calendar 覆盖 → 显式失败
    # （add_trading_days 内部对超范围日期保守 fallback 为可交易日，此处精确复核）
    for due_str in due_dates.values():
        due = date.fromisoformat(due_str)
        try:
            chinese_calendar.is_workday(due)
        except (NotImplementedError, ValueError):
            raise DueDatesComputationError(f"due date beyond calendar coverage: {due}")
    return due_dates


async def run_predict(
    trace: MarketTraceResult, snapshot: MarketTraceSnapshot
) -> PredictionRunResult:
    """对已溯源的因果链推演影响持续性（状态化契约，S2）。

    门禁：attribution_status ∈ {confirmed, hypothesis} 才预测；
    其余返回 gate_skipped。失败不再静默返回 None，而是按原因分类返回状态：
    - LLM 调用异常 → llm_failed（瞬时失败，可重试）；
    - 载荷解析/校验失败 → parse_failed（LLM 输出质量问题）；
    - 到期日计算失败 → due_dates_failed（G7：不再静默降级 {}）；
    - 未预期异常（非上述四类）→ logger.error + 重新抛出（不吞 bug，
      上层消费者/端点负责兜底）。
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
        allowed = _collect_allowed_evidence_ids(trace, snapshot)
        # P1-1：证据 ID 过滤而非一票否决（对齐 run_chat_prediction）——单一幻觉不丢整体
        filtered = [sid for sid in prediction.evidence_ids if sid in allowed]
        if len(filtered) != len(prediction.evidence_ids):
            logger.warning(
                "prediction.evidence_filtered",
                dropped=len(prediction.evidence_ids) - len(filtered),
            )
        prediction = prediction.model_copy(update={"evidence_ids": filtered})
        # 到期日计算（G7 修复）：失败显式标记 due_dates_failed，不再静默降级 {}
        try:
            due_dates = _compute_due_dates(snapshot.trade_date, prediction.horizons)
        except DueDatesComputationError as exc:
            logger.warning("prediction.due_dates_failed", error=str(exc))
            return PredictionRunResult(status="due_dates_failed", due_dates={}, reason=str(exc))
        return PredictionRunResult(status="ok", prediction=prediction, due_dates=due_dates)
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


async def predict_from_trace(
    trace_id: str, trade_date: str
) -> tuple[PredictionRunResult, dict[str, object] | None]:
    """大盘溯源后接预测的独立执行入口（PR-A/T3）。

    流程：缓存直读 → DB 重建 → snapshot.trade_date 校验 → run_predict → 按状态落库。
    trace_id 当前用于日志标识（source_id 统一为 review:{trade_date}，与 review 内联落库一致）；
    后续个股溯源/事件传导复用本入口时，trace_id 可作为溯源标识扩展点。

    落库规则：
    - ok → 完整 prediction 记录（status 不传，Node 默认 pending）；
    - gate_skipped / due_dates_failed → status=skipped + skip_reason（硬约束 3 闭环）；
    - llm_failed / parse_failed → 瞬时失败不落库（由调用方决定重试），record=None。

    Returns:
        (run_result, save_prediction 返回的 record 或 None)。
    """
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
    elif result.status in {"gate_skipped", "due_dates_failed"}:
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
    - 到期日计算已移除（A3，2026-08-12）：chat 预测 v1 不落库、返回值无验证对照，
      调用结果被丢弃属死代码；V2 落库验证时恢复（见下方注释）。
    任一失败返回 None（"永不 500"铁律）。不产生交易指令。
    """
    gate_reason = _gate_chat_snapshot(snapshot)
    if gate_reason is not None:
        logger.info("chat_prediction.gate_skip", reason=gate_reason)
        return None
    try:
        prompt_input = _build_chat_prediction_input(snapshot, news, context)
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
        allowed = _collect_chat_evidence_ids(snapshot, news)
        prediction = prediction.model_copy(
            update={
                "prediction_status": "hypothesis",
                "evidence_ids": [sid for sid in prediction.evidence_ids if sid in allowed],
            }
        )
        # A3（2026-08-12 验收裁决）：到期日计算 v1 无消费方（chat 预测不落库、返回值
        # 无验证对照）——调用结果被丢弃属死代码，移除；V2 落库验证时恢复
        # _compute_due_dates(str(snapshot["trade_date"]), prediction.horizons)
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
