"""predict_stock 单元测试 — 个股预判统一入口（Spec D 同构 · 个股预判环/迭代）。

production（落 stock_prediction pending）+ REPLAY（从 case.meta 重建，验证驱动迭代
回放）。mock 形态对齐 test_prediction_sector_service（get_quick_think 工厂 +
with_structured_output → structured.ainvoke）。
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.services import prediction_service as ps

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


def _stock_prediction(*, target: str = "600519") -> PredictionResult:
    return PredictionResult(
        schema_version="3.0",
        prediction_status="hypothesis",
        horizons=[{
            "horizon": "short",
            "remaining_estimate": "1-3 日",
            "phase": "peaking",
            "direction": "bullish",
            "target": target,
            "metric_projection": "相对现价区间波动",
            "confidence": "medium",
        }],
        evolution_narrative="个股事件驱动短线偏强",
        risks=[],
        evidence_ids=[f"stock:{target}"],
    )


@pytest.mark.asyncio
async def test_predict_stock_invokes_llm_and_persists_pending() -> None:
    """个股预判：6 位裸码 → LLM structured → 落 stock_prediction（status 缺省=pending）。"""
    llm, structured_ainvoke = _make_llm(_stock_prediction())
    with (
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_stock(
            report_date=_REPORT_DATE, stock_code="600519", stock_snapshot={}
        )
    assert out is not None
    assert out.prediction_status == "hypothesis"
    structured_ainvoke.assert_awaited_once()
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert prompt_input["input_mode"] == "stock_snapshot_driven"
    assert prompt_input["stock"]["code"] == "600519"
    assert prompt_input["stock_evidence_id"] == "stock:600519"
    mock_save.assert_awaited_once()
    payload = mock_save.await_args.args[0]
    assert payload["source_type"] == "stock_prediction"
    assert payload["source_id"] == "stock:600519:2026-07-16"
    assert "status" not in payload  # 缺省 pending → 16:00 到期验证


@pytest.mark.asyncio
async def test_predict_stock_normalizes_suffixed_ts_code() -> None:
    """带后缀 ts_code（600519.SH，light_predict/Target.internal_id 形态）→ 归一裸码落库。"""
    llm, _ = _make_llm(_stock_prediction())
    with (
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(
            ps.node_api, "save_prediction", AsyncMock(return_value={"id": "p1"})
        ) as mock_save,
    ):
        out = await ps.predict_stock(
            report_date=_REPORT_DATE, stock_code="600519.SH", stock_snapshot={}
        )
    assert out is not None
    payload = mock_save.await_args.args[0]
    assert payload["source_id"] == "stock:600519:2026-07-16"


@pytest.mark.asyncio
async def test_predict_stock_rejects_non_stock_target() -> None:
    """非个股 target（指数别名/板块名/中文名）→ None 不产出、不落库、不调 LLM。"""
    llm, _ = _make_llm(_stock_prediction())
    for bad in ("上证指数", "存储板块", "贵州茅台"):
        with (
            patch.object(ps, "get_quick_think", return_value=llm),
            patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
        ):
            out = await ps.predict_stock(
                report_date=_REPORT_DATE, stock_code=bad, stock_snapshot={}
            )
        assert out is None, bad
        mock_save.assert_not_awaited()


@pytest.mark.asyncio
async def test_predict_stock_llm_failure_returns_none() -> None:
    """LLM 失败 → None（永不 500）。"""
    llm, _ = _make_llm(None, side_effect=RuntimeError("llm down"))
    with (
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_stock(
            report_date=_REPORT_DATE, stock_code="600519", stock_snapshot={}
        )
    assert out is None
    mock_save.assert_not_awaited()


# ---------- predict_stock REPLAY 转调（个股验证驱动迭代回放） ----------


def _write_replay_case(data_dir: Path, *, trade_date: str, target: str = "600519") -> str:
    """写个股回放切片（prediction_verified_scan meta 形状：chat/light 均可）。"""
    case_id = "case_stock_replay_meta"
    case = {
        "case_id": case_id,
        "agent_id": "stock_prediction",
        "event_title": f"预判验证 {target}（{trade_date}）",
        "meta": {
            "record_id": "pred-1",
            "target": target,
            "trade_date": trade_date,
            "prediction": {
                "schema_version": "3.0",
                "prediction_status": "hypothesis",
                "horizons": [
                    {"horizon": "short", "direction": "bullish", "target": target}
                ],
                "risks": [],
                "evidence_ids": [],
            },
            "verification": {
                "short": {"result": "hit", "horizon": "short", "actual": "+1.50%"}
            },
            "t_window": "prediction",
        },
    }
    path = Path(data_dir) / "cases" / "stock_prediction" / f"{case_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(case, ensure_ascii=False), encoding="utf-8")
    return case_id


@pytest.mark.asyncio
async def test_predict_stock_replay_reconstructs_from_case_meta(
    monkeypatch: pytest.MonkeyPatch, iterate_data_dir: Path,
) -> None:
    """REPLAY_CASE_ID → predict_stock 顶部转调：从 case meta 重建、不落库、注入验证反馈。"""
    case_id = _write_replay_case(iterate_data_dir, trade_date=_REPORT_DATE)
    monkeypatch.setenv("REPLAY_CASE_ID", case_id)
    llm, structured_ainvoke = _make_llm(_stock_prediction())
    with (
        patch.object(ps, "get_quick_think", return_value=llm),
        patch.object(ps.node_api, "save_prediction", AsyncMock()) as mock_save,
    ):
        out = await ps.predict_stock(
            report_date=_REPORT_DATE, stock_code="600519", stock_snapshot={}
        )
    assert out is not None
    mock_save.assert_not_awaited()  # 回放只读
    prompt_input = json.loads(structured_ainvoke.await_args.args[0][1].content)
    assert prompt_input["replay"] is True
    assert prompt_input["target"] == "600519"
    assert prompt_input["stock"]["code"] == "600519"
    assert prompt_input["recorded_prediction"]["prediction_status"] == "hypothesis"
    assert prompt_input["verification_feedback"] == [
        {"result": "hit", "horizon": "short", "actual": "+1.50%"}
    ]


@pytest.mark.asyncio
async def test_predict_stock_replay_trade_date_mismatch_raises(
    monkeypatch: pytest.MonkeyPatch, iterate_data_dir: Path,
) -> None:
    """回放态 meta.trade_date 与入参不一致 → TraceUnavailableError（防切片错位）。"""
    case_id = _write_replay_case(iterate_data_dir, trade_date="2026-08-01")
    monkeypatch.setenv("REPLAY_CASE_ID", case_id)
    with pytest.raises(ps.TraceUnavailableError):
        await ps.predict_stock(
            report_date=_REPORT_DATE, stock_code="600519", stock_snapshot={}
        )
