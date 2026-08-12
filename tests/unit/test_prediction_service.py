import json
from datetime import date
from unittest.mock import AsyncMock, patch

import pytest

from aistock_agent.schemas.market_trace import MarketTraceResult, MarketTraceSnapshot
from aistock_agent.services.prediction_service import (
    PredictionRunResult,
    render_prediction_markdown,
    run_chat_prediction,
    run_predict,
)


def _make_snapshot() -> MarketTraceSnapshot:
    return MarketTraceSnapshot(
        snapshot_id="snap-1",
        trade_date="2026-08-10",
        captured_at=date(2026, 8, 10),
        a_share={"indices": [], "sectors": {}},
        sources={},
        missing_fields=[],
        phenomenon_discovery={
            "status": "detected",
            "primary": {
                "kind": "broad_decline",
                "summary": "大盘普跌",
                "fact_ids": ["m1"],
                "tags": [],
                "severity": "high",
            },
            "concurrent_phenomena": [],
            "data_readiness": {
                "market_data": "complete",
                "attribution_inputs": "complete",
                "causal_evidence": "ready",
            },
            "diagnostics": [],
        },
    )


def _make_trace(attribution_status="confirmed") -> MarketTraceResult:
    return MarketTraceResult(
        schema_version="1.1",
        attribution_status=attribution_status,
        candidates=[],
        primary_chain_id=None,
        alternative_chain_id=None,
        confidence="high" if attribution_status == "confirmed" else "low",
        unresolved_questions=[],
    )


_VALID_LLM_JSON = """{
  "schema_version": "1.0",
  "prediction_status": "confirmed",
  "horizons": [
    {"horizon": "short", "remaining_estimate": "1-3 日", "phase": "decaying",
     "direction": "bearish", "target": "上证指数", "metric_projection": "短期弱震荡",
     "confidence": "high"},
    {"horizon": "mid", "remaining_estimate": "2-4 周", "phase": "peaking",
     "direction": "bearish", "target": "上证指数", "metric_projection": "指数区间下移",
     "confidence": "medium"}
  ],
  "evolution_narrative": "短线已兑现大半，中线延续，长线回归",
  "risks": [{"factor": "政策对冲", "invalidation": "超预期政策落地则失效"}],
  "evidence_ids": ["m1"],
  "attribution_summary": "利空影响短线衰减、中线延续"
}"""


@pytest.mark.asyncio
async def test_run_predict_returns_run_result_with_due_dates():
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=_VALID_LLM_JSON)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert isinstance(result, PredictionRunResult)
    assert result.prediction.prediction_status == "confirmed"
    # 2026-08-10(周一) + 5 交易日 = 2026-08-17；+20 交易日跨周末
    assert result.due_dates["short"] == date(2026, 8, 17).isoformat()
    assert len(result.due_dates) == 2


@pytest.mark.asyncio
async def test_run_predict_skips_insufficient_attribution():
    result = await run_predict(_make_trace("insufficient"), _make_snapshot())
    assert result is None


@pytest.mark.asyncio
async def test_run_predict_rejects_unknown_evidence():
    bad = _VALID_LLM_JSON.replace('"m1"', '"not-exist"')
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=bad)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert result is None  # 校验失败降级，不抛异常


@pytest.mark.asyncio
async def test_run_predict_falls_back_on_llm_error():
    llm = AsyncMock()
    llm.ainvoke.side_effect = RuntimeError("llm down")
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert result is None


def test_render_prediction_markdown():
    from aistock_agent.schemas.prediction import EvolutionStep, PredictionHorizon, PredictionResult, PredictionRisk

    prediction = PredictionResult(
        schema_version="1.0",
        prediction_status="confirmed",
        horizons=[PredictionHorizon(
            horizon="mid",
            remaining_estimate="2-4 周",
            phase="peaking",
            direction="bullish",
            target="上证指数",
            metric_projection="上证指数维持 3500-3600 区间",
            confidence="medium",
        )],
        evolution_narrative="中线延续",
        risks=[PredictionRisk(factor="政策转向", invalidation="宽松转紧失效")],
        evidence_ids=["m1"],
        attribution_summary="政策利好传导 2-4 周",
    )
    md = render_prediction_markdown(prediction)
    assert "## 影响持续性预判" in md
    assert "中线(1-4周)" in md
    assert "政策转向" in md
    assert "- 演化路径：中线延续" in md


def test_render_prediction_markdown_uses_evolution_steps_when_present():
    # B2 结构化演化路径：有 evolution_steps 时逐条渲染，不再整段输出 narrative
    from aistock_agent.schemas.prediction import EvolutionStep, PredictionHorizon, PredictionResult, PredictionRisk

    prediction = PredictionResult(
        schema_version="1.0",
        prediction_status="confirmed",
        horizons=[PredictionHorizon(
            horizon="short",
            remaining_estimate="1-3 日",
            phase="decaying",
            direction="bearish",
            target="上证指数",
            metric_projection="短线弱势震荡",
            confidence="high",
        )],
        evolution_narrative="短线情绪宣泄后，市场转向关注财政补贴",
        evolution_steps=[
            EvolutionStep(label="短线", text="情绪宣泄后弱势震荡"),
            EvolutionStep(label="中线", text="市场转向关注财政补贴实际到账"),
        ],
        risks=[],
        evidence_ids=["m1"],
    )
    md = render_prediction_markdown(prediction)
    assert "## 影响持续性预判" in md
    assert "短线：情绪宣泄后弱势震荡" in md
    assert "中线：市场转向关注财政补贴实际到账" in md
    assert "- 演化路径：短线情绪宣泄后" not in md


# ---------- run_chat_prediction（Phase 4-1 对话内预测，无溯源入口） ----------


def _make_chat_snapshot(**overrides: object) -> dict:
    """对话内预测输入快照（quote/flow 对齐 stock_snapshot/capital_flow raw 结构）。"""
    snapshot = {
        "symbol": "600519",
        "trade_date": "2026-08-10",
        "quote": {"name": "贵州茅台", "price": 1500.0, "change_pct": 2.5},
        "flow": {"main_in": 120000000.0, "main_out": 80000000.0, "net_amount": 40000000.0},
    }
    snapshot.update(overrides)
    return snapshot


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "snapshot_kwargs",
    [
        {"quote": {}},
        {"quote": None},
        {"trade_date": ""},
        {"trade_date": "not-a-date"},
    ],
)
async def test_run_chat_prediction_gate_returns_none_on_missing_key_fields(snapshot_kwargs):
    """门禁：快照缺行情关键字段（quote 非空、trade_date 可解析）→ None 不调 LLM。

    flow 为可选（指数无个股资金流属"不适用"而非"缺失"），不再构成门禁字段。
    """
    llm = AsyncMock()
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(**snapshot_kwargs), [], {})
    assert result is None
    llm.ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_chat_prediction_without_flow_passes_gate():
    """指数快照（仅 quote、无 flow）→ 门禁通过、LLM 被调用、evidence 集合不含 flow id。"""
    llm = AsyncMock()
    chat_json = _VALID_LLM_JSON.replace('"m1"', '"quote:000001"')
    llm.ainvoke.return_value = AsyncMock(content=chat_json)
    snapshot = _make_chat_snapshot(symbol="000001")
    snapshot.pop("flow", None)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_chat_prediction(snapshot, [], {})
    assert result is not None
    assert result.prediction_status == "hypothesis"
    assert result.evidence_ids == ["quote:000001"]
    llm.ainvoke.assert_awaited()
    prompt_input = json.loads(llm.ainvoke.await_args.args[0][1].content)
    assert "capital_flow" not in prompt_input  # 指数无个股资金流 → LLM 输入不含 capital_flow 块


@pytest.mark.asyncio
async def test_run_chat_prediction_empty_flow_treated_as_absent():
    """flow 存在但为空 dict → 视同缺失（指数场景），门禁通过不降级。"""
    llm = AsyncMock()
    chat_json = _VALID_LLM_JSON.replace('"m1"', '"quote:600519"')
    llm.ainvoke.return_value = AsyncMock(content=chat_json)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(flow={}), [], {})
    assert result is not None
    assert result.prediction_status == "hypothesis"


@pytest.mark.asyncio
async def test_run_chat_prediction_forces_hypothesis_status():
    """后处理强制 hypothesis：LLM 输出 confirmed 也降为 hypothesis（无溯源链不得 confirmed）。"""
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=_VALID_LLM_JSON)  # prediction_status=confirmed
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(), [], {})
    assert result is not None
    assert result.prediction_status == "hypothesis"


@pytest.mark.asyncio
async def test_run_chat_prediction_filters_evidence_ids_to_input_items():
    """evidence_ids 只取输入快照/新闻存在项：编造 id 被过滤，不抛错（run_predict 是 raise）。"""
    chat_json = _VALID_LLM_JSON.replace(
        '"m1"', '"quote:600519", "flow:600519", "news:1", "made-up-id"'
    )
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=chat_json)
    news = [{"evidence_id": "news:1", "title": "贵州茅台提价公告"}]
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(), news, {})
    assert result is not None
    assert result.evidence_ids == ["quote:600519", "flow:600519", "news:1"]


@pytest.mark.asyncio
async def test_run_chat_prediction_falls_back_on_llm_error():
    """LLM 失败 → None（'永不 500'铁律，与 run_predict 一致）。"""
    llm = AsyncMock()
    llm.ainvoke.side_effect = RuntimeError("llm down")
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(), [], {})
    assert result is None
