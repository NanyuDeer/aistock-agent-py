import json
from collections.abc import Callable
from datetime import date
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from aistock_agent.schemas.market_trace import (
    MarketTraceResult,
    MarketTraceSnapshot,
    ReviewArtifact,
)
from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.services.prediction_service import (
    PredictionRunResult,
    TraceUnavailableError,
    predict_from_trace,
    render_prediction_markdown,
    run_chat_prediction,
    run_predict,
    save_skipped_prediction,
)


def _make_snapshot(trade_date="2026-08-10") -> MarketTraceSnapshot:
    return MarketTraceSnapshot(
        snapshot_id="snap-1",
        trade_date=trade_date,
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
    assert result.status == "ok"
    assert result.prediction is not None
    assert result.prediction.prediction_status == "confirmed"
    # 2026-08-10(周一) + 5 交易日 = 2026-08-17；+20 交易日跨周末
    assert result.due_dates["short"] == date(2026, 8, 17).isoformat()
    assert len(result.due_dates) == 2


@pytest.mark.asyncio
async def test_run_predict_gate_skipped_on_insufficient_attribution():
    result = await run_predict(_make_trace("insufficient"), _make_snapshot())
    assert result.status == "gate_skipped"
    assert result.prediction is None
    assert result.reason == "attribution_status=insufficient"


@pytest.mark.asyncio
async def test_run_predict_filters_unknown_evidence(capsys):
    """P1-1：证据 ID 幻觉被过滤而非一票否决——预测仍产出，只保留 allowed 子集并告警。"""
    bad = _VALID_LLM_JSON.replace('"m1"', '"m1", "made-up-id"')
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=bad)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert isinstance(result, PredictionRunResult)
    assert result.status == "ok"
    assert result.prediction is not None
    assert result.prediction.evidence_ids == ["m1"]  # 只保留 allowed 子集
    assert "prediction.evidence_filtered" in capsys.readouterr().out  # 告警频率可观察


@pytest.mark.asyncio
async def test_run_predict_due_dates_failure_raises_status(monkeypatch):
    """G7 修复：add_trading_days 抛异常 → 显式 due_dates_failed（不再静默降级 {}）。"""
    def _boom(*args: object, **kwargs: object) -> date:
        raise NotImplementedError("no available data for year 2027")

    monkeypatch.setattr(
        "aistock_agent.services.prediction_service.add_trading_days", _boom
    )
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=_VALID_LLM_JSON)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert result.status == "due_dates_failed"
    assert result.due_dates == {}
    assert result.prediction is None
    assert "due date" in result.reason


@pytest.mark.asyncio
async def test_run_predict_calendar_coverage_guard_fails_due_dates():
    """覆盖守卫：chinese_calendar.is_workday 抛 NotImplementedError（due date 超 2004-2026）
    → 显式 due_dates_failed，不再用近似日期静默产出。"""
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=_VALID_LLM_JSON)
    with patch(
        "aistock_agent.services.prediction_service.chinese_calendar.is_workday",
        side_effect=NotImplementedError("no holiday data for 2027"),
    ):
        with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
            result = await run_predict(_make_trace(), _make_snapshot())
    assert result.status == "due_dates_failed"
    assert result.due_dates == {}
    assert result.prediction is None


@pytest.mark.asyncio
async def test_run_predict_reraises_unexpected_errors():
    """未预期异常（非 LLM/parse/due_dates 四类）→ logger.error + 重新抛出，不静默吞 bug。"""
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=_VALID_LLM_JSON)
    with patch(
        "aistock_agent.services.prediction_service._compute_due_dates",
        side_effect=RuntimeError("unexpected boom"),
    ):
        with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
            with pytest.raises(RuntimeError, match="unexpected boom"):
                await run_predict(_make_trace(), _make_snapshot())


@pytest.mark.asyncio
async def test_run_predict_injects_missing_schema_version():
    """Bug A 双保险：LLM 缺 schema_version → 注入 1.0 后校验通过（834ddf9 之外再兜底）。"""
    bad = _VALID_LLM_JSON.replace('  "schema_version": "1.0",\n', "")
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=bad)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert isinstance(result, PredictionRunResult)
    assert result.status == "ok"
    assert result.prediction is not None
    assert result.prediction.schema_version == "1.0"


@pytest.mark.asyncio
async def test_run_predict_drops_extra_keys():
    """P1-2：LLM 输出 thinking/analysis 等多余键 → 剔除后校验通过（extra=forbid 不再炸）。"""
    extra = _VALID_LLM_JSON.replace(
        '  "schema_version": "1.0",\n',
        '  "schema_version": "1.0",\n  "thinking": "先分析再输出",\n  "analysis": {"a": 1},\n',
    )
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=extra)
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert isinstance(result, PredictionRunResult)
    assert result.status == "ok"
    assert result.prediction is not None
    assert result.prediction.prediction_status == "confirmed"


@pytest.mark.asyncio
async def test_run_predict_extracts_json_from_fence_and_prefix():
    """LLM 输出带 ```json 围栏或前缀文本 → 仍提取成功。"""
    for raw in (
        "```json\n" + _VALID_LLM_JSON + "\n```",
        "好的，以下是预测结果：\n" + _VALID_LLM_JSON,
    ):
        llm = AsyncMock()
        llm.ainvoke.return_value = AsyncMock(content=raw)
        with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
            result = await run_predict(_make_trace(), _make_snapshot())
        assert isinstance(result, PredictionRunResult)
        assert result.status == "ok"
        assert result.prediction is not None
        assert result.prediction.prediction_status == "confirmed"


@pytest.mark.asyncio
async def test_run_predict_pure_text_returns_parse_failed():
    """LLM 输出完全非法（纯文本）→ status=parse_failed（失败原因可区分，非 None）。"""
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content="抱歉，我无法生成预测。")
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert result.status == "parse_failed"
    assert result.prediction is None


@pytest.mark.asyncio
async def test_run_predict_llm_failed_on_llm_error():
    llm = AsyncMock()
    llm.ainvoke.side_effect = RuntimeError("llm down")
    with patch("aistock_agent.services.prediction_service.get_deep_think", return_value=llm):
        result = await run_predict(_make_trace(), _make_snapshot())
    assert result.status == "llm_failed"
    assert result.prediction is None
    assert "llm down" in result.reason


# ---------- predict_from_trace（独立入口：缓存直读 → DB 重建 → run_predict → 落库） ----------


def _make_review_artifact_dict(trade_date="2026-08-10") -> dict[str, object]:
    """构造合法 ReviewArtifact 的 dict 表示（对齐 set_cached_review 缓存内容）。"""
    artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=_make_snapshot(trade_date),
        trace=_make_trace(),
        markdown="# md",
        trace_summary="sum",
        sectors=[],
    )
    return artifact.model_dump(mode="json")


def _make_db_report_dict(trade_date="2026-08-10") -> dict[str, object]:
    """构造 DB 落库 report dict（对齐 _build_review_report 的 content.market_trace 结构）。"""
    return {
        "content": {
            "market_trace": {
                "snapshot": _make_snapshot(trade_date).model_dump(mode="json"),
                "trace": _make_trace().model_dump(mode="json"),
            }
        }
    }


def _make_ok_llm() -> AsyncMock:
    llm = AsyncMock()
    llm.ainvoke.return_value = AsyncMock(content=_VALID_LLM_JSON)
    return llm


@pytest.mark.asyncio
async def test_predict_from_trace_cache_hit_persists_ok():
    """缓存直读路径：合法 artifact → run_predict ok → save_prediction 被调且 payload 正确。"""
    llm = _make_ok_llm()
    with patch(
        "aistock_agent.services.prediction_service.get_cached_review",
        AsyncMock(return_value=_make_review_artifact_dict()),
    ) as mock_cache:
        with patch("aistock_agent.services.prediction_service.node_api") as mock_api:
            mock_api.get_analysis_report = AsyncMock()
            mock_api.save_prediction = AsyncMock(return_value={"id": 1})
            with patch(
                "aistock_agent.services.prediction_service.get_deep_think", return_value=llm
            ):
                result, record = await predict_from_trace("trace-1", "2026-08-10")
    mock_cache.assert_awaited_once_with("2026-08-10")
    mock_api.get_analysis_report.assert_not_awaited()  # 缓存命中不查 DB
    assert result.status == "ok"
    assert record == {"id": 1}
    payload = mock_api.save_prediction.await_args.args[0]
    assert payload["source_type"] == "market_trace"
    assert payload["source_id"] == "review:2026-08-10"
    assert payload["schema_version"] == "1.0"
    assert payload["prediction"]["prediction_status"] == "confirmed"
    assert payload["due_dates"]["short"] == "2026-08-17"
    assert "status" not in payload  # ok 不传 status，Node 默认 pending


@pytest.mark.asyncio
async def test_predict_from_trace_db_rebuild_path():
    """DB 重建路径：缓存未命中 → get_analysis_report 重建 trace/snapshot → run_predict 落库。"""
    llm = _make_ok_llm()
    with patch(
        "aistock_agent.services.prediction_service.get_cached_review",
        AsyncMock(return_value=None),
    ):
        with patch("aistock_agent.services.prediction_service.node_api") as mock_api:
            mock_api.get_analysis_report = AsyncMock(return_value=_make_db_report_dict())
            mock_api.save_prediction = AsyncMock(return_value={"id": 2})
            with patch(
                "aistock_agent.services.prediction_service.get_deep_think", return_value=llm
            ):
                result, record = await predict_from_trace("trace-1", "2026-08-10")
    mock_api.get_analysis_report.assert_awaited_once_with("review", "2026-08-10")
    assert result.status == "ok"
    assert record == {"id": 2}
    payload = mock_api.save_prediction.await_args.args[0]
    assert payload["source_id"] == "review:2026-08-10"


@pytest.mark.asyncio
async def test_predict_from_trace_trade_date_mismatch_raises():
    """trade_date 校验：snapshot.trade_date != 参数 trade_date → TraceUnavailableError。"""
    with patch(
        "aistock_agent.services.prediction_service.get_cached_review",
        AsyncMock(return_value=_make_review_artifact_dict(trade_date="2026-08-09")),
    ):
        with pytest.raises(TraceUnavailableError, match="trade_date"):
            await predict_from_trace("trace-1", "2026-08-10")


@pytest.mark.asyncio
async def test_predict_from_trace_both_sources_unavailable_raises():
    """两条读取链都失败（缓存 None + DB None）→ TraceUnavailableError。"""
    with patch(
        "aistock_agent.services.prediction_service.get_cached_review",
        AsyncMock(return_value=None),
    ):
        with patch("aistock_agent.services.prediction_service.node_api") as mock_api:
            mock_api.get_analysis_report = AsyncMock(return_value=None)
            with pytest.raises(TraceUnavailableError, match="2026-08-10"):
                await predict_from_trace("trace-1", "2026-08-10")


@pytest.mark.asyncio
async def test_predict_from_trace_gate_skipped_persists_skipped():
    """gate_skipped → 落 skipped 记录：status=skipped、prediction.skip_reason、due_dates={}。"""
    artifact = ReviewArtifact(
        schema_version="1.1",
        snapshot=_make_snapshot(),
        trace=_make_trace("insufficient"),
        markdown="# md",
        trace_summary="sum",
        sectors=[],
    )
    with patch(
        "aistock_agent.services.prediction_service.get_cached_review",
        AsyncMock(return_value=artifact.model_dump(mode="json")),
    ):
        with patch("aistock_agent.services.prediction_service.node_api") as mock_api:
            mock_api.save_prediction = AsyncMock(return_value={"id": 3})
            result, record = await predict_from_trace("trace-1", "2026-08-10")
    assert result.status == "gate_skipped"
    assert record == {"id": 3}
    payload = mock_api.save_prediction.await_args.args[0]
    assert payload["status"] == "skipped"
    assert payload["prediction"] == {"skip_reason": "attribution_status=insufficient"}
    assert payload["due_dates"] == {}
    assert payload["source_type"] == "market_trace"
    assert payload["source_id"] == "review:2026-08-10"
    assert payload["schema_version"] == "1.0"


@pytest.mark.asyncio
async def test_predict_from_trace_llm_failed_does_not_persist():
    """llm_failed → 瞬时失败不落库（由调用方决定重试），record=None。"""
    llm = AsyncMock()
    llm.ainvoke.side_effect = RuntimeError("llm down")
    with patch(
        "aistock_agent.services.prediction_service.get_cached_review",
        AsyncMock(return_value=_make_review_artifact_dict()),
    ):
        with patch("aistock_agent.services.prediction_service.node_api") as mock_api:
            mock_api.save_prediction = AsyncMock(return_value={"id": 4})
            with patch(
                "aistock_agent.services.prediction_service.get_deep_think", return_value=llm
            ):
                result, record = await predict_from_trace("trace-1", "2026-08-10")
    assert result.status == "llm_failed"
    assert record is None
    mock_api.save_prediction.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_skipped_prediction_payload():
    """save_skipped_prediction：独立导出的 skipped 落库辅助（供 TraceUnavailableError 场景）。"""
    with patch("aistock_agent.services.prediction_service.node_api") as mock_api:
        mock_api.save_prediction = AsyncMock(return_value={"id": 9})
        record = await save_skipped_prediction("review:2026-08-10", "no trace available")
    assert record == {"id": 9}
    payload = mock_api.save_prediction.await_args.args[0]
    assert payload["status"] == "skipped"
    assert payload["prediction"] == {"skip_reason": "no trace available"}
    assert payload["due_dates"] == {}
    assert payload["schema_version"] == "1.0"


def test_render_prediction_markdown():
    from aistock_agent.schemas.prediction import (
        PredictionHorizon,
        PredictionResult,
        PredictionRisk,
    )

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
    from aistock_agent.schemas.prediction import (
        EvolutionStep,
        PredictionHorizon,
        PredictionResult,
    )

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


def _make_chat_llm(
    prediction: PredictionResult | None = None,
    *,
    side_effect: Callable[[object], object] | Exception | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """构造新调用链 mock（对齐 test_qa_router 手法）：get_quick_think 工厂 →
    with_chat_structured_output(json_mode) → structured.ainvoke 直接产出已解析的
    PredictionResult，或按 side_effect 抛异常（如 pydantic ValidationError）。
    """
    llm = MagicMock()
    structured_ainvoke = AsyncMock()
    structured_ainvoke.return_value = prediction
    if side_effect is not None:
        structured_ainvoke.side_effect = side_effect
    llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=structured_ainvoke)
    )
    return llm, structured_ainvoke


def _chat_prediction(text: str) -> PredictionResult:
    """把合法 LLM 输出文本解析为 PredictionResult（结构化输出返回对象而非文本）。"""
    return PredictionResult.model_validate_json(text)


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
    llm, structured_ainvoke = _make_chat_llm()
    with patch("aistock_agent.services.prediction_service.get_quick_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(**snapshot_kwargs), [], {})
    assert result is None
    structured_ainvoke.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_chat_prediction_without_flow_passes_gate():
    """指数快照（仅 quote、无 flow）→ 门禁通过、LLM 被调用、evidence 集合不含 flow id。"""
    llm, structured_ainvoke = _make_chat_llm(
        prediction=_chat_prediction(_VALID_LLM_JSON.replace('"m1"', '"quote:000001"'))
    )
    snapshot = _make_chat_snapshot(symbol="000001")
    snapshot.pop("flow", None)
    with patch("aistock_agent.services.prediction_service.get_quick_think", return_value=llm):
        result = await run_chat_prediction(snapshot, [], {})
    assert result is not None
    assert result.prediction_status == "hypothesis"
    assert result.evidence_ids == ["quote:000001"]
    structured_ainvoke.assert_awaited()
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert "capital_flow" not in prompt_input  # 指数无个股资金流 → LLM 输入不含 capital_flow 块


@pytest.mark.asyncio
async def test_run_chat_prediction_empty_flow_treated_as_absent():
    """flow 存在但为空 dict → 视同缺失（指数场景），门禁通过不降级。"""
    llm, _ = _make_chat_llm(
        prediction=_chat_prediction(_VALID_LLM_JSON.replace('"m1"', '"quote:600519"'))
    )
    with patch("aistock_agent.services.prediction_service.get_quick_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(flow={}), [], {})
    assert result is not None
    assert result.prediction_status == "hypothesis"


@pytest.mark.asyncio
async def test_run_chat_prediction_forces_hypothesis_status():
    """后处理强制 hypothesis：LLM 输出 confirmed 也降为 hypothesis（无溯源链不得 confirmed）。"""
    llm, _ = _make_chat_llm(prediction=_chat_prediction(_VALID_LLM_JSON))  # status=confirmed
    with patch("aistock_agent.services.prediction_service.get_quick_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(), [], {})
    assert result is not None
    assert result.prediction_status == "hypothesis"


@pytest.mark.asyncio
async def test_run_chat_prediction_filters_evidence_ids_to_input_items():
    """evidence_ids 只取输入快照/新闻存在项：编造 id 被过滤，不抛错（run_predict 是 raise）。"""
    chat_json = _VALID_LLM_JSON.replace(
        '"m1"', '"quote:600519", "flow:600519", "news:1", "made-up-id"'
    )
    llm, _ = _make_chat_llm(prediction=_chat_prediction(chat_json))
    news = [{"evidence_id": "news:1", "title": "贵州茅台提价公告"}]
    with patch("aistock_agent.services.prediction_service.get_quick_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(), news, {})
    assert result is not None
    assert result.evidence_ids == ["quote:600519", "flow:600519", "news:1"]


@pytest.mark.asyncio
async def test_run_chat_prediction_missing_schema_version_degrades():
    """LLM 输出缺 schema_version → 结构化解析抛 ValidationError → 返回 None（永不 500）。

    Phase 4-1 冒烟实测根因：PREDICTION_CHAT_PROMPT 未要求输出 schema_version，
    而 PredictionResult.schema_version 是必填 Literal["1.0"] → 线上恒降级。
    本测试锁定新调用链（json_mode 结构化输出）的降级语义：缺字段走异常 → None，
    skill 层落到 degraded 提示而非 500。
    """
    bad_json = _VALID_LLM_JSON.replace('  "schema_version": "1.0",\n', "")
    with pytest.raises(ValidationError):
        PredictionResult.model_validate_json(bad_json)  # 夹具自证：缺 schema_version 必校验失败

    async def _structured_validate(_messages: object) -> PredictionResult:
        # 模拟 with_structured_output(json_mode) 内部 pydantic 校验：LLM 文本缺字段
        return PredictionResult.model_validate_json(bad_json)

    llm, _ = _make_chat_llm(side_effect=_structured_validate)
    with patch("aistock_agent.services.prediction_service.get_quick_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(), [], {})
    assert result is None


@pytest.mark.asyncio
async def test_run_chat_prediction_falls_back_on_llm_error():
    """LLM 失败 → None（'永不 500'铁律，与 run_predict 一致）。"""
    llm, _ = _make_chat_llm(side_effect=RuntimeError("llm down"))
    with patch("aistock_agent.services.prediction_service.get_quick_think", return_value=llm):
        result = await run_chat_prediction(_make_chat_snapshot(), [], {})
    assert result is None


@pytest.mark.asyncio
async def test_run_chat_prediction_no_due_dates_call(monkeypatch):
    """A3：run_chat_prediction 不再调用 _compute_due_dates（v1 无消费方）。

    原 test_run_chat_prediction_survives_due_dates_out_of_range（B4 冒烟根因：long 档
    +120 交易日落 2027 超出 holiday 日历）随 A3 死代码移除而失效——到期日计算已删除，
    改为断言不再调用（best-effort try/except 整段移除）。
    """
    called = []
    monkeypatch.setattr(
        "aistock_agent.services.prediction_service._compute_due_dates",
        lambda *a, **k: called.append(1),
    )
    llm, structured_ainvoke = _make_chat_llm(prediction=_chat_prediction(_VALID_LLM_JSON))
    with patch(
        "aistock_agent.services.prediction_service.get_quick_think", return_value=llm
    ):
        result = await run_chat_prediction(_make_chat_snapshot(), [], {})
    assert result is not None
    assert result.prediction_status == "hypothesis"
    structured_ainvoke.assert_awaited()
    assert called == []
