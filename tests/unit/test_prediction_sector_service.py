"""Spec D · Task D5：板块预判入口（predict_sector）+ 大盘溯源级联（_market_trace_brief）。

级联 = 输入组装（内部拉当日 review 结论作上下文，非事件驱动）。predict_sector 复用
run_chat_prediction 的 LLM structured 骨架（quick_think + with_chat_structured_output →
structured.ainvoke 直接产出已解析 PredictionResult），mock 形态对齐既有
test_prediction_service.py 对 run_chat_prediction 的 mock 方式（patch get_quick_think
工厂 + mock with_structured_output 返回 Runnable），assert_awaited 命中真实调用点。
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.services import prediction_service as ps

_RESOLVED: dict[str, str] = {"ts_code": "BK1001", "name": "存储板块"}
_SECTOR_SNAPSHOT: dict[str, object] = {"sector": {"name": "存储板块"}}
_REPORT_DATE = "2026-07-16"


def _make_llm(
    prediction: PredictionResult | None,
    *,
    side_effect: Exception | None = None,
) -> tuple[MagicMock, AsyncMock]:
    """构造 quick_think 工厂 mock：with_chat_structured_output → structured.ainvoke。"""
    llm = MagicMock()
    structured_ainvoke = AsyncMock()
    structured_ainvoke.return_value = prediction
    if side_effect is not None:
        structured_ainvoke.side_effect = side_effect
    llm.with_structured_output = MagicMock(
        return_value=MagicMock(ainvoke=structured_ainvoke)
    )
    return llm, structured_ainvoke


def _sector_prediction(
    *,
    prediction_status: str = "hypothesis",
    evidence_ids: list[str] | None = None,
    metric_projection: str = "相对现价区间波动",
) -> PredictionResult:
    return PredictionResult(
        schema_version="3.0",
        prediction_status=prediction_status,
        horizons=[{
            "horizon": "short",
            "remaining_estimate": "1-3 日",
            "phase": "peaking",
            "direction": "bearish",
            "target": "存储板块",
            "metric_projection": metric_projection,
            "confidence": "medium",
        }],
        evolution_narrative="大盘情绪传导，板块短线弱势震荡后回稳",
        risks=[],
        evidence_ids=evidence_ids if evidence_ids is not None else [],
    )


# ---------- _market_trace_brief（大盘 review 结论摘要，级联输入组装） ----------


@pytest.mark.asyncio
async def test_market_trace_brief_returns_review_summary() -> None:
    """review 持久化 content.display_report.summary 可读取 → 返回摘要串。"""
    with patch.object(
        ps.node_api,
        "get_analysis_report",
        AsyncMock(return_value={
            "content": {"display_report": {"summary": "半导体产业链暴跌"}}
        }),
    ) as mock_report:
        brief = await ps._market_trace_brief(_REPORT_DATE)
    assert brief == "半导体产业链暴跌"
    mock_report.assert_awaited_once_with(report_type="review", report_date=_REPORT_DATE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("report_return", "side_effect"),
    [
        (None, None),  # review 报告不存在
        ({"content": {}}, None),  # 缺 display_report
        ({"content": {"display_report": {}}}, None),  # 缺 summary
        ({"content": {"display_report": {"summary": ""}}}, None),  # 空摘要
        (None, RuntimeError("boom")),  # 读取异常
    ],
)
async def test_market_trace_brief_degrades_to_empty(
    report_return: dict[str, object] | None,
    side_effect: Exception | None,
) -> None:
    """大盘结论缺失/结构不符/读取异常 → 返回 ""（级联降级，不阻断板块预判）。"""
    mock = AsyncMock(side_effect=side_effect) if side_effect is not None else AsyncMock(
        return_value=report_return
    )
    with patch.object(ps.node_api, "get_analysis_report", mock):
        brief = await ps._market_trace_brief(_REPORT_DATE)
    assert brief == ""


# ---------- predict_sector（板块预判入口，级联输入组装） ----------


@pytest.mark.asyncio
async def test_predict_sector_unresolved_target_returns_none() -> None:
    """板块 target 解析失败（resolve_sector_target → None）→ None，不调 LLM、不落库。"""
    with (
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=None)),
        patch.object(ps, "_market_trace_brief", AsyncMock()) as mock_brief,
        patch.object(ps, "get_quick_think") as mock_llm,
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is None
    mock_llm.assert_not_called()
    mock_brief.assert_not_awaited()
    mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_predict_sector_invokes_llm_and_persists() -> None:
    """板块预判：构造级联输入 → LLM structured → 落 prediction_records（sector_prediction）。"""
    llm, structured_ainvoke = _make_llm(
        _sector_prediction(evidence_ids=["sector:BK1001"])
    )
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="半导体产业链暴跌")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"
    assert out.evidence_ids == ["sector:BK1001"]  # 确定性 sector:{ts_code} 可引用
    structured_ainvoke.assert_awaited_once()
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert prompt_input["market_trace_brief"] == "半导体产业链暴跌"  # 级联上下文并入
    assert prompt_input["sector_snapshot"] == _SECTOR_SNAPSHOT
    assert prompt_input["sector"] == {
        "kind": "sector", "internal_id": "BK1001", "code": "BK1001", "name": "存储板块",
    }
    mock_save.assert_awaited_once()
    payload = mock_save.await_args.args[0]
    assert payload["source_type"] == "sector_prediction"
    assert payload["source_id"] == "sector:存储板块:2026-07-16"
    assert payload["schema_version"] == "3.0"
    assert payload["prediction"]["prediction_status"] == "hypothesis"
    assert "short" in payload["due_dates"]
    assert "due_dates_approximate" not in payload


@pytest.mark.asyncio
async def test_predict_sector_forces_hypothesis_and_filters_evidence() -> None:
    """LLM 输出 confirmed + 编造证据 id → 强制 hypothesis、evidence 只留输入存在项。"""
    llm, _ = _make_llm(
        _sector_prediction(
            prediction_status="confirmed",
            evidence_ids=["sector:BK1001", "made-up-id"],
        )
    )
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"  # 无溯源链不得 confirmed
    assert out.evidence_ids == ["sector:BK1001"]  # 编造 id 被过滤而非抛错


@pytest.mark.asyncio
async def test_predict_sector_allows_explicit_snapshot_evidence_ids() -> None:
    """快照内显式携带 evidence_id 的条目可被引用（对齐 chat news 项语义）。"""
    llm, _ = _make_llm(
        _sector_prediction(evidence_ids=["sector:BK1001", "ths:BK1001", "made-up"])
    )
    snapshot = {
        "sector": {"name": "存储板块"},
        "items": [{"evidence_id": "ths:BK1001", "pct_chg": -3.2}],
    }
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE, sector_name="存储板块", sector_snapshot=snapshot
        )
    assert out is not None
    assert out.evidence_ids == ["sector:BK1001", "ths:BK1001"]


@pytest.mark.asyncio
async def test_predict_sector_redacts_absolute_point() -> None:
    """P0-3 红线：板块预判不产绝对点位——命中 → 剥离（_hard_validate_chat_prediction）。"""
    llm, _ = _make_llm(_sector_prediction(metric_projection="板块指数 5000 点上方运行"))
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert "5000" not in out.horizons[0].metric_projection


@pytest.mark.asyncio
async def test_predict_sector_persist_failure_still_returns_prediction() -> None:
    """落库失败仅 warning 不阻断（永不 500）：save_prediction 抛异常 → 仍返回 prediction。"""
    llm, _ = _make_llm(_sector_prediction())
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(side_effect=RuntimeError("db down"))
        ),
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"


@pytest.mark.asyncio
async def test_predict_sector_llm_failure_returns_none() -> None:
    """LLM 调用失败 → None（永不 500，对齐 run_chat_prediction 契约）。"""
    llm, _ = _make_llm(None, side_effect=RuntimeError("llm down"))
    with (
        patch.object(ps, "_market_trace_brief", AsyncMock(return_value="")),
        patch.object(ps, "resolve_sector_target", AsyncMock(return_value=dict(_RESOLVED))),
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_sector(
            report_date=_REPORT_DATE,
            sector_name="存储板块",
            sector_snapshot=_SECTOR_SNAPSHOT,
        )
    assert out is None
    mock_save.assert_not_awaited()
