"""Task 4：归一化强制层 apply_horizon_policy（spec 2026-09-03-动态档位 §5.4）。

确定性强制层在 model_validate 后、due_dates 计算前调用：
越界裁剪 / short 恒产校验 / required 缺档 degraded + omitted 留痕。
final fix（2026-09-03）：degraded 不提级 insufficient；板块文本 fallback 上限；
driver helper（trace/sector）单测。
"""
from types import SimpleNamespace

import pytest

from aistock_agent.prompts.workers.prediction import (
    PREDICTION_CHAT_PROMPT,
    PREDICTION_PROMPT,
)
from aistock_agent.schemas.market_trace import CandidateExplanation
from aistock_agent.schemas.prediction import PredictionResult
from aistock_agent.services.prediction_service import (
    _extract_driver_for_sector,
    _extract_driver_for_trace,
    _inject_horizon_policy,
    apply_horizon_policy,
)


def _mini_result(
    horizons: list[str], prediction_status: str = "confirmed",
) -> PredictionResult:
    """最小合法 PredictionResult（与 Task2 同构造风格；字段以 schemas/prediction.py 为准）。"""
    return PredictionResult(
        schema_version="3.0",
        prediction_status=prediction_status,  # type: ignore[arg-type]
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


def test_required_missing_keeps_insufficient_status():
    # 大盘入口 LLM 自判 insufficient（证据不足、仅 short、confidence low）→ required(mid/long)
    # 缺档只做 degraded 留痕（omitted），不得把 insufficient 提级为 hypothesis（final fix）
    r = _mini_result(["short"], prediction_status="insufficient")
    out = apply_horizon_policy(r, "policy_macro", "index")
    assert out.prediction_status == "insufficient"
    assert {o.horizon for o in out.omitted_horizons} == {"mid", "long"}


def test_policy_crop_empty_raises_for_insufficient_fallback():
    # LLM 结构性漏 short（白名单恒含 short 却只给 mid/long）→ 裁剪后无档，拒绝落库抛 ValueError，
    # 由调用方既有异常兜底（对齐 parse_failed 语义 → 落 insufficient，不留脏 pending）。
    r = _mini_result(["mid", "long"])
    with pytest.raises(ValueError, match="no horizon left after policy"):
        apply_horizon_policy(r, "transient_market", "sector")


# Task 4b：prompt 白名单运行时注入（spec 2026-09-03-动态档位 §5.2 系统注入）。
# _inject_horizon_policy 把 PREDICTION_PROMPT/PREDICTION_CHAT_PROMPT 的占位段
# "{driver_type} 型 → required=[...] / optional=[...]" 替换为实例化白名单。


def test_inject_horizon_policy_index_policy_macro():
    out = _inject_horizon_policy(PREDICTION_PROMPT, "policy_macro", "index")
    assert "policy_macro 型" in out
    assert "required=[short, mid, long]" in out
    assert "optional=[]" in out
    assert "{driver_type}" not in out
    assert "required=[...]" not in out


def test_inject_horizon_policy_sector_transient_market():
    out = _inject_horizon_policy(PREDICTION_CHAT_PROMPT, "transient_market", "sector")
    assert "transient_market 型" in out
    assert "required=[short]" in out
    assert "optional=[]" in out
    assert "{driver_type}" not in out
    assert "required=[...]" not in out


# ===== Task4 final fix：driver helper 提取与设防（spec §5.1 归类入口）=====


def _candidate(cid: str, category: str, status: str = "supported") -> CandidateExplanation:
    return CandidateExplanation(
        id=cid,
        category=category,  # type: ignore[arg-type]  # Literal 由测试显式给值，避免构造样板
        status=status,  # type: ignore[arg-type]
        verdict="x",
        chain=None,
        supporting_evidence_ids=["m1"],
        counter_evidence_ids=[],
    )


def _trace_with(candidates: list[CandidateExplanation], primary_chain_id: str | None):
    return SimpleNamespace(candidates=candidates, primary_chain_id=primary_chain_id)


def test_extract_driver_for_trace_primary_enum_mapping():
    # 主路径：primary_chain_id 命中候选 → 英文枚举精确映射（review 4 类）
    trace = _trace_with(
        [
            _candidate("c-sup", "industry_technology_supply", status="weak"),
            _candidate("c-primary", "domestic_macro_policy"),
        ],
        primary_chain_id="c-primary",
    )
    assert _extract_driver_for_trace(trace) == "policy_macro"


def test_extract_driver_for_trace_fallback_when_primary_empty():
    # 回落路径：primary_chain_id 为空（hypothesis 被 review 服务层清空）→ 首个 supported 候选
    trace = _trace_with(
        [
            _candidate("c-a", "market_positioning_liquidity"),
            _candidate("c-b", "industry_technology_supply"),
        ],
        primary_chain_id=None,
    )
    assert _extract_driver_for_trace(trace) == "sector_rotation"


def test_extract_driver_for_trace_fallback_unknown_to_transient():
    # 回落路径：无 supported 候选 / 候选类别未知名 → classify_driver 保守 transient_market
    trace = _trace_with([_candidate("c-r", "domestic_macro_policy", status="rejected")],
                        primary_chain_id=None)
    assert _extract_driver_for_trace(trace) == "transient_market"
    assert _extract_driver_for_trace(_trace_with([], None)) == "transient_market"


def test_extract_driver_for_sector_text_policy_capped_to_trend():
    # 文本 fallback 命中 policy/宏观强词（大盘政策主因摘要）→ 上限 trend_fundamental，
    # 不得强制板块硬产 long（policy_macro required long → trend_fundamental optional long）
    ctx = {"market_trace_brief": "今日市场主因是政策利好释放，宏观面转暖"}
    assert _extract_driver_for_sector(ctx) == "trend_fundamental"


def test_extract_driver_for_sector_structured_category_uncapped():
    # 结构化 driver_category 通道保留原样：板块自身类别可精确到 policy_macro（不截断）
    ctx = {"driver_category": "产业政策加码", "market_trace_brief": "今日市场主因是政策利好"}
    assert _extract_driver_for_sector(ctx) == "policy_macro"


def test_extract_driver_for_sector_fallback_sector_rotation():
    # 回落路径：brief 未知名（非白名单强词）或为空 → sector_rotation（short+mid，无长期逻辑）
    assert _extract_driver_for_sector({"market_trace_brief": "两市缩量整理"}) == "sector_rotation"
    assert _extract_driver_for_sector({}) == "sector_rotation"
    assert _extract_driver_for_sector({"market_brief": "   "}) == "sector_rotation"
