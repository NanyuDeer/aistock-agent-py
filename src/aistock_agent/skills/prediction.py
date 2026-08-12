"""prediction Skill（Phase 4-1）— 对话内影响持续性推演（现状快照驱动）。

复用 tools 的 get_quote / get_capital_flow 并发取数（ainvoke，LangChain
StructuredTool 契约），组 snapshot dict 调 services.prediction_service.
run_chat_prediction——LLM 结构化输出（json_mode）已封装在 service 内，
skill 不直接调 LLM。数据缺失 / 门禁不过 / LLM 失败 → degraded Evidence
（facts 复用 PREDICT_DEGRADED_HINT 语义）。不产生交易指令；
prediction_status 恒 hypothesis（由 run_chat_prediction 后处理保证）。
"""
from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from typing import Any

from aistock_agent.prompts.general.system import PREDICT_DEGRADED_HINT
from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.services.data_client import node_api
from aistock_agent.services.prediction_service import run_chat_prediction
from aistock_agent.skills.base import skill
from aistock_agent.tools.stock_tools import get_capital_flow, get_quote
from aistock_agent.utils.date import shanghai_today

# 固定免责声明（spec，用户已确认）——不产生交易指令红线的展示层收口
DISCLAIMER = "以上为模型推演，仅供参考，不构成投资建议。"
# 低置信提示（spec）：任意档位 confidence=low 时追加
LOW_CONFIDENCE_HINT = "市场变化快，该判断不确定性较高。"

# 数据源未返回时的固定字样（与 stock_snapshot/capital_flow 的 _EMPTY_MARKERS 对齐）
_EMPTY_MARKERS = ("未找到股票", "行情数据为空", "资金流向数据为空", "数据不可用")

# get_quote 文本契约：【名称】最新价: X  涨跌幅: Y%
_QUOTE_RE = re.compile(
    r"【(?P<name>.+?)】最新价: (?P<price>\S+)  涨跌幅: (?P<pct>[+-]?\d+(?:\.\d+)?)%"
)
# get_capital_flow 文本契约：主力流入: X  主力流出: Y\n主力净流入: Z
_FLOW_RE = re.compile(r"主力流入: (\S+)  主力流出: (\S+)\n主力净流入: (\S+)")

_HORIZON_LABEL = {"short": "短线(1-5交易日)", "mid": "中线(1-4周)", "long": "长线(1-6月)"}
_PHASE_LABEL = {
    "building": "影响形成",
    "peaking": "影响高峰",
    "decaying": "影响衰减",
    "returning": "回归常态",
}
_DIRECTION_LABEL = {"bullish": "看多", "bearish": "看空", "neutral": "中性"}


def _parse_quote(text: str, symbol: str) -> dict[str, object]:
    """get_quote 文本 → quote payload（英文键对齐 stock_snapshot raw.quote）。"""
    m = _QUOTE_RE.search(text)
    if not m:
        return {}
    return {
        "name": m.group("name"),
        "code": symbol,
        "price": m.group("price"),
        "change_pct": m.group("pct"),
    }


def _parse_flow(text: str) -> dict[str, object]:
    """get_capital_flow 文本 → flow payload（英文键对齐 capital_flow raw.flow）。"""
    m = _FLOW_RE.search(text)
    if not m:
        return {}
    return {
        "main_in": m.group(1),
        "main_out": m.group(2),
        "net_amount": m.group(3),
        "flow_5d": [],
    }


def _degraded_evidence(symbol: str, reason: str) -> Evidence:
    """门禁不过/数据缺失共用降级 Evidence（facts 复用 PREDICT_DEGRADED_HINT 语义）。"""
    return Evidence(
        facts=[PREDICT_DEGRADED_HINT],
        sources=[],
        as_of=datetime.now(UTC),
        symbols=[symbol],
        degraded=True,
        degraded_reason=reason,
        skill_name="prediction",
        raw={},
    )


def _render_facts(
    result: PredictionResult, symbol: str, quote_text: str, flow_text: str
) -> list[str]:
    """三段式文本：现状快照 → 影响持续性推演（hypothesis 标注）→ 风险 + 免责声明。

    指数场景（flow_text 为空，无个股资金流）→ 现状行不渲染空"资金："段，
    以"指数无个股资金流数据"注明（"不适用"而非"缺失"）。
    """
    low_confidence = any(h.confidence == "low" for h in result.horizons)
    horizon_lines = [
        (
            f"- {_HORIZON_LABEL.get(h.horizon, h.horizon)}（{h.remaining_estimate}）："
            f"阶段{_PHASE_LABEL.get(h.phase, h.phase)}，"
            f"方向{_DIRECTION_LABEL.get(h.direction, h.direction)}，"
            f"置信{h.confidence}；验证对象 {h.target}，预期 {h.metric_projection}"
        )
        for h in result.horizons
    ]
    if flow_text:
        flow_display = flow_text.replace(chr(10), "；")
        status_line = f"【{symbol} 现状】行情：{quote_text}；资金：{flow_display}"
    else:
        status_line = f"【{symbol} 现状】行情：{quote_text}（指数无个股资金流数据）"
    facts = [
        status_line,
        f"【影响持续性推演（假设推演）】{result.attribution_summary or '基于现状快照推演如下：'}",
        *horizon_lines,
    ]
    if result.evolution_narrative:
        facts.append(f"演化路径：{result.evolution_narrative}")
    for risk in result.risks:
        facts.append(f"风险提示：{risk.factor}——{risk.invalidation}")
    if low_confidence:
        facts.append(LOW_CONFIDENCE_HINT)
    facts.append(DISCLAIMER)
    return facts


@skill
async def prediction(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    """对话内影响持续性推演：取数（quote+flow）→ run_chat_prediction → 三段式 facts。

    入参兼容 ``symbols: ["6位代码"]``（_build_default_skill_call 后续传入）与
    既有单标的 ``symbol``，并兜底 goal.symbols。news 可选透传。
    指数路径仅由显式 ``args.index_name`` 触发（闸门 1 指数短路透传；不能靠代码
    判定——000001 同时是上证指数与平安银行），走 node_api 指数行情，
    不取个股资金流（指数无该数据，属"不适用"而非"缺失"）。
    """
    symbols = list(dict.fromkeys(args.get("symbols") or []))
    symbol = args.get("symbol") or (
        symbols[0] if symbols else (goal.symbols[0] if goal.symbols else "")
    )
    if not symbol:
        raise ValueError("prediction requires 'symbol' in args or goal.symbols")

    index_name = args.get("index_name")
    is_index = index_name is not None
    quote_payload: dict[str, object]
    flow_payload: dict[str, object] | None = None
    quote_text: str
    flow_text: str = ""
    if is_index:
        # 指数语义：get_quote("000001") 会命中平安银行（个股）而非上证指数，
        # 且指数无个股资金流 → 改走 node_api 指数行情，flow 整体跳过
        data = await node_api.get("/internal/index/quotes?symbols=" + symbol)
        matched = None
        if isinstance(data, dict) and isinstance(data.get("indices"), list):
            matched = next(
                (
                    i
                    for i in data["indices"]
                    if isinstance(i, dict) and i.get("index") == symbol
                ),
                None,
            )
        if matched is None:
            return _degraded_evidence(symbol, "指数行情取数异常/无该指数")
        name = matched.get("name") or matched.get("index") or symbol
        price = matched.get("price")
        pct = matched.get("changePercent")
        if price is None or pct is None:
            return _degraded_evidence(symbol, "指数行情字段缺失")
        quote_payload = {"name": name, "code": symbol, "price": price, "change_pct": pct}
        if isinstance(pct, int | float):
            quote_text = f"{name}({symbol}) 最新价 {price} 涨跌幅 {pct:+.2f}%"
        else:
            quote_text = f"{name}({symbol}) 最新价 {price}"
    else:
        quote_raw, flow_raw = await asyncio.gather(
            get_quote.ainvoke({"symbol": symbol}),
            get_capital_flow.ainvoke({"symbol": symbol}),
            return_exceptions=True,
        )
        if isinstance(quote_raw, BaseException) or isinstance(flow_raw, BaseException):
            return _degraded_evidence(symbol, "行情/资金取数异常")

        quote_text, flow_text = str(quote_raw), str(flow_raw)
        if any(m in quote_text for m in _EMPTY_MARKERS) or any(
            m in flow_text for m in _EMPTY_MARKERS
        ):
            return _degraded_evidence(symbol, "数据源未返回行情/资金")

        quote_payload = _parse_quote(quote_text, symbol)
        flow_payload = _parse_flow(flow_text)
        if not quote_payload or not flow_payload:
            return _degraded_evidence(symbol, "行情/资金字段解析失败")

    now = datetime.now(UTC)
    snapshot: dict[str, object] = {
        "symbol": symbol,
        "trade_date": shanghai_today().isoformat(),
        "quote": quote_payload,
        "quote_evidence_id": f"quote:{symbol}",
    }
    if flow_payload is not None:
        snapshot["flow"] = flow_payload
        snapshot["flow_evidence_id"] = f"flow:{symbol}"
    news = args.get("news") if isinstance(args.get("news"), list) else []
    context = {"question": goal.question, "time_range": goal.time_range}
    result = await run_chat_prediction(snapshot, news, context)
    if result is None:
        return _degraded_evidence(symbol, "门禁不过/LLM 未产出预测")

    sources: list[ChatSource] = [
        ChatSource(
            source_id=f"quote:{symbol}:{now.isoformat()}",
            kind="realtime_quote",
            title=f"{symbol} 实时行情",
            snippet=quote_text[:200],
            captured_at=now,
        ),
    ]
    if flow_text:
        sources.append(
            ChatSource(
                source_id=f"flow:{symbol}:{now.isoformat()}",
                kind="capital_flow",
                title=f"{symbol} 资金流向",
                snippet=flow_text[:200],
                captured_at=now,
            )
        )

    return Evidence(
        facts=_render_facts(result, symbol, quote_text, flow_text),
        sources=sources,
        as_of=now,
        symbols=[symbol],
        degraded=False,
        degraded_reason=None,
        skill_name="prediction",
        raw={"symbol": symbol, "snapshot": snapshot, "prediction": result.model_dump(mode="json")},
    )
