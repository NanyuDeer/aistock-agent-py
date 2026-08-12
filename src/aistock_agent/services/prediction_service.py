"""预测能力执行服务 — 影响持续性推演。

独立可复用推理包：输入溯源结果 + 事实快照，输出 PredictionResult（含到期日）。
大盘溯源（review）内联调用；个股溯源/事件传导后续接入同一入口。
失败一律返回 None 不抛异常，保证调用方主流程不受阻断（"永不 500"）。
"""

import json
import re
from dataclasses import dataclass
from datetime import date

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.prompts.workers.prediction import (
    PREDICTION_CHAT_PROMPT,
    PREDICTION_PROMPT,
)
from aistock_agent.schemas.market_trace import MarketTraceResult, MarketTraceSnapshot
from aistock_agent.schemas.prediction import PredictionHorizon, PredictionResult
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

# 代码围栏剥离 — 防御 LLM 可能包裹的 ```json ... ```
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?\s*```\s*$", re.DOTALL)


@dataclass(frozen=True)
class PredictionRunResult:
    """预测执行结果：预测工件 + 各档位到期交易日。"""

    prediction: PredictionResult
    due_dates: dict[str, str]  # {horizon: YYYY-MM-DD}


def _strip_code_fences(text: str) -> str:
    m = _CODE_FENCE_RE.match(text.strip())
    if m:
        return m.group(1).strip()
    return text.strip()


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
    base = date.fromisoformat(trade_date)
    return {
        h.horizon: add_trading_days(base, HORIZON_TRADING_DAY_OFFSETS[h.horizon]).isoformat()
        for h in horizons
    }


async def run_predict(
    trace: MarketTraceResult, snapshot: MarketTraceSnapshot
) -> PredictionRunResult | None:
    """对已溯源的因果链推演影响持续性。

    门禁：attribution_status ∈ {confirmed, hypothesis} 才预测；
    insufficient/not_applicable 返回 None。任一失败返回 None（调用方降级为无预测章节）。
    """
    if trace.attribution_status not in {"confirmed", "hypothesis"}:
        logger.info("prediction_skip_by_attribution_status", status=trace.attribution_status)
        return None
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
        prediction = PredictionResult.model_validate_json(_strip_code_fences(raw_text))
        allowed = _collect_allowed_evidence_ids(trace, snapshot)
        for sid in prediction.evidence_ids:
            if sid not in allowed:
                raise ValueError(f"prediction evidence not in trace: {sid}")
        due_dates = _compute_due_dates(snapshot.trade_date, prediction.horizons)
        return PredictionRunResult(prediction=prediction, due_dates=due_dates)
    except Exception as exc:
        logger.warning("prediction_run_failed", error=str(exc), exc_info=True)
        return None


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
    - 到期日由 _compute_due_dates 确定性计算（LLM 不输出日期，与 run_predict 同源）。
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
        # 到期日确定性计算：校验 trade_date 与 horizon 档位映射（异常 → 降级 None）
        _compute_due_dates(str(snapshot["trade_date"]), prediction.horizons)
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
