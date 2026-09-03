"""Task 4：归一化强制层 apply_horizon_policy（spec 2026-09-03-动态档位 §5.4）。

确定性强制层在 model_validate 后、due_dates 计算前调用：
越界裁剪 / short 恒产校验 / required 缺档 degraded + omitted 留痕。
"""
import pytest

from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.services.prediction_service import apply_horizon_policy


def _mini_result(horizons: list[str]) -> PredictionResult:
    """最小合法 PredictionResult（与 Task2 同构造风格；字段以 schemas/prediction.py 为准）。"""
    return PredictionResult(
        schema_version="3.0", prediction_status="confirmed",
        horizons=[
            {"horizon": h, "remaining_estimate": "2-4 周", "phase": "building",
             "direction": "bullish", "target": "上证指数", "metric_projection": "+2%",
             "confidence": "medium"}
            for h in horizons
        ],
        evolution_narrative="x", risks=[], evidence_ids=["evt-1"],
    )


def test_transient_market_crops_long():
    r = _mini_result(["short", "mid", "long"])
    out = apply_horizon_policy(r, "transient_market", "sector")
    assert [h.horizon for h in out.horizons] == ["short"]
    assert {o.horizon for o in out.omitted_horizons} == {"mid", "long"}


def test_policy_macro_keeps_three_and_no_omitted():
    r = _mini_result(["short", "mid", "long"])
    out = apply_horizon_policy(r, "policy_macro", "index")
    assert [h.horizon for h in out.horizons] == ["short", "mid", "long"]
    assert out.omitted_horizons == []


def test_required_missing_degrades_to_hypothesis():
    # policy_macro 型 LLM 只给 short：缺 required(mid/long) → 不硬补，degraded(hypothesis)+omitted
    r = _mini_result(["short"])
    out = apply_horizon_policy(r, "policy_macro", "index")
    assert out.prediction_status == "hypothesis"
    assert {o.horizon for o in out.omitted_horizons} == {"mid", "long"}


def test_policy_crop_empty_raises_for_insufficient_fallback():
    # LLM 结构性漏 short（白名单恒含 short 却只给 mid/long）→ 裁剪后无档，拒绝落库抛 ValueError，
    # 由调用方既有异常兜底（对齐 parse_failed 语义 → 落 insufficient，不留脏 pending）。
    r = _mini_result(["mid", "long"])
    with pytest.raises(ValueError, match="no horizon left after policy"):
        apply_horizon_policy(r, "transient_market", "sector")
